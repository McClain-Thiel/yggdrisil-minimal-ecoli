import base64
import hashlib
import json
import netrc
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import boto3
from yggdrisil_archive_io import read_checked

root = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
out = root / 'runs/literature-ablation-kimi-continuation-20261106'
bucket = 'phd-research-storage-1758274488'
prefix = 'yggdrisil/experiments/vecoli-kimi-continuation-20gen-3seed-20261106/'
s3 = boto3.client('s3', region_name='us-east-1')
owner = boto3.client('sts', region_name='us-east-1').get_caller_identity()['Account']
s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=owner)
assert all(s3.get_public_access_block(Bucket=bucket,ExpectedBucketOwner=owner)['PublicAccessBlockConfiguration'].values())
secrets = []
# Include the existing tracking login without printing or exporting credentials.
netrc_path = Path.home() / '.netrc'
if netrc_path.exists():
    for login, account, password in netrc.netrc(netrc_path).hosts.values():
        if password:
            secrets.append(password.encode())
def collect(value, key=''):
    if isinstance(value, dict):
        for k, v in value.items(): collect(v, k)
    elif isinstance(value, list):
        for v in value: collect(v, key)
    elif isinstance(value, str) and len(value) > 15 and any(k in key.upper() for k in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
        secrets.append(value.encode())
with ThreadPoolExecutor(max_workers=4) as pool:
    for value in pool.map(lambda p: json.loads(read_checked(p)), (root / 'runs').glob('*/credentials/*.json')):
        collect(value)
for line in read_checked(root / '.env').decode().splitlines():
    if '=' in line and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        collect(value.strip().strip('\"\''), key)

def upload(data, category):
    json.loads(data)
    if any(secret in data for secret in secrets) or re.search(rb'sk-or-v1-[a-fA-F0-9]{32,}|(?:AKIA|ASIA)[A-Z0-9]{16}|\.platformv01\.[A-Za-z0-9_-]{30,}', data):
        raise RuntimeError('Credential pattern in final metadata')
    digest=hashlib.sha256(data).hexdigest()
    key=prefix+category+'/'+digest+'.json'
    s3.put_object(Bucket=bucket,Key=key,Body=data,ServerSideEncryption='AES256',ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode())
    with s3.get_object(Bucket=bucket,Key=key)['Body'] as stream:
        returned=stream.read()
    assert returned==data
    return {'uri':f's3://{bucket}/{key}','sha256':digest,'bytes':len(data)}


refs={}
for name in ['completion-record-publication.json','final-analysis-artifact.json','resource-cleanup.json']:
 data=read_checked(out/name);refs[name]=upload(data,'completion')
ledger=read_checked(root/'experiments.json');assert ledger==read_checked(Path('/private/tmp/yggdrisil-migration-ledger.json'))
refs['experiments.json']=upload(ledger,'ledger');assert read_checked(root/'experiments.json')==ledger
result={'status':'completed_with_exceptions_verified','published_at':datetime.now(timezone.utc).isoformat(),'records':refs,'full_round_trip_verified':True,'full_configured_secret_scan':True,'monitor_deleted':True}
(out/'final-publication.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
