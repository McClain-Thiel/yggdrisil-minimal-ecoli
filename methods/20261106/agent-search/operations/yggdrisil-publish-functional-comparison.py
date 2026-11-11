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
prefix = 'yggdrisil/experiments/literature-ablation-kimi-continuation-20261106/'
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


ledger_path=root/'experiments.json'
mirror_path=Path('/private/tmp/yggdrisil-migration-ledger.json')
before=read_checked(ledger_path);mirror_before=read_checked(mirror_path)
assert before==mirror_before, 'Ledger copies differ; reconcile before publishing'
ledger=json.loads(before)
report_bytes=read_checked(out/'functional-gene-comparison.json')
report=json.loads(report_bytes)
mapping_bytes=read_checked(out/'functional-gene-mapping.json')
assert hashlib.sha256(mapping_bytes).hexdigest()==report['mapping_file']['sha256']
assert report['method']['shared_functional_gene_count']==1197
assert report['method']['current_functional_gene_count']==1915
refs={'report':upload(report_bytes,'analysis'),'mapping':upload(mapping_bytes,'analysis')}
ids=[report['search_experiment_id'],report['validation_experiment_id']]
for entry in ledger['experiments']:
 if entry['id'] in ids:
  entry['deletion_comparability']={
   'analyzed_at':report['analyzed_at'],**refs,
   'functional_definition':'Gherman2025 five-process accounting rule; current fitted vEcoli model has1915 genes; exact intersection with published functional panel has1197 genes.',
   'current_functional_denominator':1915,'shared_functional_denominator':1197,
   'candidates':[{k:f[k] for k in ['name','state_id','deletion_set_sha256','full_registry','current_functional_set','shared_functional_set']} for f in report['finalists'] if f['state_id'] is not None],
   'published_reference':report['published_reference'],
   'essentiality_coverage':report['essentiality'],
   'interpretation':'Best candidate deletes789/1915=41.20% under current functional definition, but422/1197=35.25% on shared functional genes versus published475/1197=39.68%. No claim of superior reduction on shared genes, laboratory viability, or physical DNA deletion. Validation statuses unchanged.'}
assert all(any(e['id']==i for e in ledger['experiments']) for i in ids)
assert ledger['storage_migration']['status']=='completed_verified'
ledger['updated_at']=datetime.now(timezone.utc).isoformat()
data=(json.dumps(ledger,indent=2)+'\n').encode()
assert read_checked(ledger_path)==before and read_checked(mirror_path)==mirror_before
ledger_ref=upload(data,'ledger')
assert read_checked(ledger_path)==before and read_checked(mirror_path)==mirror_before
for path in [ledger_path,mirror_path]:
 tmp=path.with_name(path.name+'.functional-analysis.tmp');tmp.write_bytes(data);tmp.replace(path)
publication={'published_at':ledger['updated_at'],**refs,'ledger':ledger_ref,'full_configured_secret_scan':True,'full_round_trip_verified':True,'scientific_statuses_unchanged':True}
(out/'functional-comparison-publication.json').write_text(json.dumps(publication,indent=2)+'\n')
print(json.dumps(publication,indent=2))
