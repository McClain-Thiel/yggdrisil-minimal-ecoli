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


protocol_path=root/'benchmarks/literature_ablation_continuation.json'
protocol_bytes=read_checked(protocol_path);protocol=json.loads(protocol_bytes)
assert protocol['authorization']['paid_launch'] and protocol['authorization']['incremental_cap_usd']==50
assert [a['model_budget_usd'] for a in protocol['arms']]==[12,12,12]
assert protocol['budget_proposal']['incremental_total_cap']==50
record=json.loads(read_checked(out/'monitoring-summary.json'))
assert record['instance_id']=='i-0512356f473be8f8d'
assert record['source_files_verified'] and record['search_checkpoint_verified']
if record['search']['status']=='running':
 assert record['search_launcher']['alive'] and record['search_launcher']['identity_matches']
elif record['search']['status']=='completed':
 assert record['search']['all_model_children_stopped'] and not record['search_launcher']['alive']
 assert all(j['status']=='completed' and j['exit_code']==0 for j in record['search']['jobs'].values())
else:
 raise RuntimeError('Unexpected search status; inspect before publishing')
assert record['validation_launcher']['alive'] and record['validation_launcher']['identity_matches']
refs={}
for name in ['monitoring-summary.json','preflight.json','source-snapshot.json','validation-readiness.json','deadlines.json','authorization.json','client-repair.json','provider-accounting.json','snapshot-repair.json','snapshot-repair-tests.json','validation-preparation-failure.json','validation-preparation-failure-2.json','native-extension-repair.json','final-search-summary.json','preparation-smoke-check.json','monitor-approval.json']:
 refs[name]=upload(read_checked(out/name),'monitoring' if name=='monitoring-summary.json' else 'provenance')
if (out/'solver-monitoring.json').exists():
 refs['solver-monitoring.json']=upload(read_checked(out/'solver-monitoring.json'),'monitoring')
for path in sorted(out.glob('model-exception-*.json')):
 refs[path.name]=upload(read_checked(path),'diagnostics')
protocol_ref=upload(protocol_bytes,'plans')
ledger_path=root/'experiments.json';before=read_checked(ledger_path);ledger=json.loads(before)
search=next(e for e in ledger['experiments'] if e['id']==protocol['experiment_id'])
validation=next(e for e in ledger['experiments'] if e['id']==protocol['validation']['experiment_id'])
for entry in [search,validation]:
 entry.update(instance_id=record['instance_id'],remote_root='/home/ubuntu/yggdrisil-continuation-20261106',public_ip='34.227.172.82',aws_region='us-east-1',authorization=protocol['authorization'],budget_proposal=protocol['budget_proposal'],deadlines=json.loads(read_checked(out/'deadlines.json')),last_verified_at=record['checked_at'],protocol_backup=protocol_ref,operational_provenance=refs)
 entry['budget_proposal']['cost_caveat']='Hard $12 model-key caps per arm and 12-hour worker limit; total additional authorization $50. Frozen plan prose retains a prior 24-hour typo; numeric settings and installed deadlines are 12 hours.'
search.update(status=record['search']['status'],data_backup=record['search_checkpoint_verified'],wandb_run_urls=[record['search']['wandb_url']]+[a['tracking']['wandb_url'] for a in record['arms'].values()],current_arms=record['arms'],launcher=record['search_launcher'],readiness='22 offline search tests and exact evaluator/source preflight passed; preserved dependency-setup failure resumed with the same keys and stage deadlines.',notes='Scientific execution remote-only. Old parent and failed attempts are preserved. Three scoped keys cap model charges at $36; disable them only after all search children stop, then reconcile settlement.')
validation['operational_repair']=json.loads(read_checked(out/'snapshot-repair.json'))
validation['preserved_preparation_failures']=[refs['validation-preparation-failure.json'],refs['validation-preparation-failure-2.json']]
validation['native_extension_repair']=json.loads(read_checked(out/'native-extension-repair.json'))
validation.update(status=record['validation']['status'],launcher=record['validation_launcher'],readiness='Validation supervisor active after verified snapshot-preparation repair; 28 validation integrity tests and 7 solver tests passed. Fixed final candidates and native knockout audit required before calibration; MG1655 two-division calibration required before 15 lineages.',remote_root='/home/ubuntu/yggdrisil-continuation-20261106/validation')
validation['workflow_checkpoints']={name:{'status':item['catalog']['status'],'data_backup':item['catalog'].get('data_backup',{}),'artifact_verified':item.get('artifact_verified',False),'verified_progress':item.get('verified_progress',[])} for name,item in record.get('validation_jobs',{}).items()}
validation['wandb_run_urls']=sorted({url for item in record.get('validation_jobs',{}).values() for url in item['catalog'].get('wandb_run_urls',[])})
validation['last_monitor_record']=refs['monitoring-summary.json']
exceptions={(e['seed'],e['genotype'],e['failed_generation']):e for e in validation.get('model_exceptions',[])}
for path in sorted(out.glob('model-exception-*.json')):
 evidence=json.loads(read_checked(path))
 assert evidence['experiment_id']==validation['id'] and evidence['classification']=='model_exception'
 assert evidence['verified_files'] and all(f['worker_s3_wandb_sha256_match'] for f in evidence['verified_files'].values())
 exceptions[(evidence['seed'],evidence['genotype'],evidence['failed_generation'])]={**evidence,'report':refs[path.name]}
if exceptions:
 validation['model_exceptions']=[exceptions[k] for k in sorted(exceptions)]
if 'solver-monitoring.json' in refs:
 solver_record=json.loads(read_checked(out/'solver-monitoring.json'))
 validation['solver_recovery_verification']={'report':refs['solver-monitoring.json'],'verified_at':solver_record['verified_at'],'progress_snapshot_at':solver_record['progress_snapshot_at'],'accepted_fallback_count':solver_record['accepted_fallback_count'],'interpretation':solver_record['interpretation']}
if record['validation'].get('calibration'):
 validation['calibration']=record['validation']['calibration']
 validation.setdefault('full_run_started_at',record['validation']['updated_at'])
validation['completed_full_cell_cycles']=sum(item['catalog'].get('data_backup',{}).get('completed_cell_cycles',0) for name,item in record.get('validation_jobs',{}).items() if name!='calibration')
parent=next(e for e in ledger['experiments'] if e['id']==protocol['parent_experiment_id']);assert parent['status']=='search_and_validation_complete_with_exceptions'
old_validation=next(e for e in ledger['experiments'] if e['id']==protocol['parent_validation_id']);assert old_validation['status']=='completed_with_exceptions_verified'
assert ledger['storage_migration']['status']=='completed_verified'
ledger['storage_policy']['model_spending']='Only explicitly approved, experiment-specific paid runs are authorized; current continuation cap is $50 additional including $36 model keys and $14 compute/storage/transfers. No blanket authority for other model, Edison or compute spending.'
search['monitoring_automation']=json.loads(read_checked(out/'monitor-approval.json'))
validation['monitoring_automation']=search['monitoring_automation']
ledger['updated_at']=datetime.now(timezone.utc).isoformat();data=(json.dumps(ledger,indent=2)+'\n').encode()
assert read_checked(ledger_path)==before
ledger_ref=upload(data,'ledger')
assert read_checked(ledger_path)==before
for path in [ledger_path,Path('/private/tmp/yggdrisil-migration-ledger.json')]:
 tmp=path.with_name(path.name+'.continuation.tmp');tmp.write_bytes(data);tmp.replace(path)
publication={'published_at':ledger['updated_at'],'status':search['status'],'validation_status':validation['status'],'protocol':protocol_ref,'records':refs,'ledger':ledger_ref,'full_configured_secret_scan':True,'full_round_trip_verified':True}
(out/'launch-publication.json').write_text(json.dumps(publication,indent=2)+'\n')
print(json.dumps(publication,indent=2))
