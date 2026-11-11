"""Read and verify the completed secondary archive on the approved primary."""
import hashlib
import json
import shlex
import subprocess
from pathlib import Path

op = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/vecoli-depth-matched-20261111')
discovery = json.loads((op / 'secondary-final-discovery.json').read_bytes())
receipt = {key: {field: discovery[key][field] for field in ['uri', 'sha256', 'bytes']} for key in ['archive', 'index']}
for ref in receipt.values():
    if not ref['uri'].startswith('s3://phd-research-storage-1758274488/yggdrisil/experiments/vecoli-depth-matched-20261111/secondary/final/'):
        raise ValueError('Unexpected final archive location')
receipt['wandb_artifact'] = 'mcclain/yggdrisil/secondary-final:v0'
worker = next(row for row in json.loads((op / 'workers-live.json').read_bytes())['workers'] if row['role'] == 'primary')
assert worker['instance_id'] == 'i-05b12ad155a4aff74'
code = '''import fcntl,hashlib,json,os,sys
from pathlib import Path
from types import SimpleNamespace
import boto3
r=Path('/home/ubuntu/yggdrisil-depth-matched-20261111')
lock=(r/'independent-checkpoint-verification.lock').open('a+')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
sys.path[:0]=[str(r/'application'),str(r/'application/src'),str(r/'framework/src')]
os.environ.update(json.loads((r/'credentials/tracking.json').read_bytes()));os.environ.pop('WANDB_SERVICE',None)
from scripts.depth_matched_validation import verify_archive
store=SimpleNamespace(s3=boto3.client('s3',region_name='us-east-1'),owner=boto3.client('sts').get_caller_identity()['Account'],bucket='phd-research-storage-1758274488')
receipt=RECEIPT
proof=verify_archive(store,receipt)
proof['verification_host_instance']='i-05b12ad155a4aff74'
proof['audited_worker_instance']='i-05bece33af7a6cbb2'
proof['method_sha256']=hashlib.sha256((r/'application/scripts/depth_matched_validation.py').read_bytes()).hexdigest()
proof['no_scientific_execution']=True
proof['wandb_state']='COMMITTED'
(r/'secondary-final-independent-verification.json').write_text(json.dumps(proof,indent=2)+'\\n')
print(json.dumps(proof))
'''.replace('RECEIPT', repr(receipt))
ssh = ['ssh', '-i', str(Path.home() / '.ssh/mcclain-phd-aws-dev.pem'), '-o', 'BatchMode=yes',
       '-o', 'ConnectTimeout=10', '-o', 'ClearAllForwardings=yes',
       '-o', 'HostKeyAlias=' + worker['ssh_host_key_alias'], 'ubuntu@' + worker['public_ip']]
raw = subprocess.check_output(ssh + ['/home/ubuntu/yggdrisil-depth-matched-20261111/env/bin/python -c ' + shlex.quote(code)])
proof = json.loads(raw)
assert proof['status'] == 'verified' and proof['members'] == discovery['members']
assert proof['receipt'] == receipt
proof['audit_launcher_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
(op / 'secondary-final-independent-verification.json').write_text(json.dumps(proof, indent=2) + '\n')
(op / 'source' / Path(__file__).name).write_bytes(Path(__file__).read_bytes())
print(json.dumps(proof, indent=2))
