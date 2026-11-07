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
protocol_bytes=read_checked(protocol_path)
protocol=json.loads(protocol_bytes)
assert protocol['authorization']['paid_launch'] is False
assert len(protocol['arms'])==3
assert {a['arm'] for a in protocol['arms']}=={'no_retrieval','frozen_dossier','adaptive_literature'}
assert sum(a['model_budget_usd'] for a in protocol['arms'])==protocol['budget_proposal']['combined_model_cap']==90
assert protocol['budget_proposal']['incremental_total_cap']==120
assert protocol['validation']['maximum_cells']==protocol['validation']['genotype_count']*len(protocol['validation']['seeds'])*protocol['validation']['generations']
assert protocol['remote_plan']['instance_id'] is None
old_protocol=json.loads(read_checked(root/'benchmarks/vecoli_20gen_remote.json'))
for arm in protocol['arms']:
    parent=next(g for g in old_protocol['genotypes'] if g['state_id']==arm['parent_best_state_id'])
    assert parent['deletion_set_sha256']==arm['parent_best_deletion_set_sha256']
    assert parent['deletion_count']==arm['parent_best_deletions']
    assert arm['max_total_states']==arm['inherited_states']+500
    assert arm['max_total_steps']==arm['inherited_steps']+750
    assert arm['agent_config']['edison'] is False
    assert arm['agent_config']['require_initial_research']==(arm['arm']=='adaptive_literature')
protocol_ref=upload(protocol_bytes,'plans')
check_bytes=read_checked(out/'checkpoint-verification.json')
checks_ref=upload(check_bytes,'verification')
ledger_path=root/'experiments.json'
old_ledger=read_checked(ledger_path)
ledger=json.loads(old_ledger)
parent=next(e for e in ledger['experiments'] if e['id']==protocol['parent_experiment_id'])
completed=next(e for e in ledger['experiments'] if e['id']==protocol['parent_validation_id'])
assert parent['status']=='search_and_validation_complete_with_exceptions'
assert completed['status']=='completed_with_exceptions_verified'
assert completed['resource_cleanup']['instance_state']=='terminated'
entry={
 'id':protocol['experiment_id'],'kind':'literature_ablation_continuation',
 'status':'prepared_awaiting_budget_approval','created_at':protocol['created_at'],
 'parent_ids':[protocol['parent_experiment_id'],protocol['parent_validation_id']],
 'protocol':'benchmarks/literature_ablation_continuation.json','data_backup':protocol_ref,
 'checkpoint_verification':checks_ref,'execution_backend':'remote_linux_only',
 'remote_root':protocol['remote_plan']['remote_root'],'instance_id':None,
 'authorization':protocol['authorization'],'budget_proposal':protocol['budget_proposal'],
 's3_prefix':protocol['tracking']['s3_prefix'],'arms':protocol['arms'],
 'followup_validation_id':protocol['validation']['experiment_id'],
 'readiness':'Protocol, costs and parent checkpoints prepared; continuation wrapper and isolated remote preflight still required before any model calls.',
 'notes':'No new model calls, credential transfers, instance or simulation launched. Existing finalized results and all failed attempts remain preserved.'
}
validation={
 'id':protocol['validation']['experiment_id'],'kind':'vecoli_continuation_validation',
 'status':'prepared_waiting_for_search_and_budget_approval','created_at':protocol['created_at'],
 'parent_ids':[protocol['experiment_id'],protocol['parent_validation_id']],
 'protocol':'benchmarks/literature_ablation_continuation.json#validation',
 'protocol_backup':protocol_ref,'authorization':protocol['authorization'],
 'budget_source':protocol['experiment_id'],'genotype_count':5,'candidate_count':3,
 'seeds':[101,211,307],'target_generations':20,'maximum_cell_simulations':300,
 'calibration_cell_simulations':2,'execution_backend':'remote_linux_only',
 's3_prefix':protocol['tracking']['validation_s3_prefix'],'instance_id':None,
 'candidate_states':'Selected and frozen only after all three search arms stop; no current candidate substitution.',
 'solver_policy':protocol['validation']['solver_policy'],
 'solver_patch_sha256':protocol['validation']['solver_patch_sha256']
}
for new_entry in (entry,validation):
    existing=next((e for e in ledger['experiments'] if e['id']==new_entry['id']),None)
    if existing is None:
        ledger['experiments'].append(new_entry)
    else:
        assert existing['status'] in ('prepared_awaiting_budget_approval','prepared_waiting_for_search_and_budget_approval')
        existing.update(new_entry)
parent['continuation_experiment_id']=protocol['experiment_id']
completed['followup_intent']['prepared_search_experiment_id']=protocol['experiment_id']
completed['followup_intent']['prepared_validation_experiment_id']=protocol['validation']['experiment_id']
ledger['updated_at']=datetime.now(timezone.utc).isoformat()
ledger_bytes=(json.dumps(ledger,indent=2)+'\n').encode()
assert read_checked(ledger_path)==old_ledger
ledger_ref=upload(ledger_bytes,'ledger')
assert read_checked(ledger_path)==old_ledger
for path in (ledger_path,Path('/private/tmp/yggdrisil-migration-ledger.json')):
    pending=path.with_name(path.name+'.continuation.tmp')
    pending.write_bytes(ledger_bytes)
    pending.replace(path)
assert read_checked(protocol_path)==protocol_bytes
publication={'experiment_id':entry['id'],'status':'prepared_awaiting_budget_approval','published_at':datetime.now(timezone.utc).isoformat(),'protocol':protocol_ref,'checkpoint_verification':checks_ref,'ledger':ledger_ref,'configured_secret_scan':'Full historical credentials, repo environment and netrc values screened; no values exported.','checks_passed':['All three exact parent genotypes match prior validation','Separate state and step offsets correct','Adaptive fresh search remains required','No live Edison','Budget and simulation counts consistent','Prior completed records preserved','All uploaded metadata fully round-trip verified']}
publication['data_backup']=upload((json.dumps(publication,indent=2)+'\n').encode(),'plans')
(out/'preparation-publication.json').write_text(json.dumps(publication,indent=2)+'\n')
print(json.dumps(publication,indent=2))
