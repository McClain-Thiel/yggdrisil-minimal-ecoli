import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import boto3
from botocore.exceptions import ClientError

root = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
out = root / 'runs/literature-ablation-kimi-continuation-20261106'
audit = json.loads((out/'final-backup-verification.json').read_text())
assert audit['status']=='verified' and len(audit['wandb_artifacts'])==4
analysis_artifact=json.loads((out/'final-analysis-artifact.json').read_text())
assert analysis_artifact['state']=='COMMITTED' and analysis_artifact['all_files_downloaded_sha256_verified']
publication=json.loads((out/'completion-record-publication.json').read_text())
assert audit['archive_verification']['members']==audit['final_archive']['file_count']
s3=boto3.client('s3',region_name='us-east-1')
for ref in [publication['records']['final-backup-verification.json'],audit['final_index'],*analysis_artifact['s3_records'].values()]:
 u=urlparse(ref['uri'])
 with s3.get_object(Bucket=u.netloc,Key=u.path.lstrip('/'))['Body'] as body:
  data=body.read()
 assert hashlib.sha256(data).hexdigest()==ref['sha256']
instance_id='i-0512356f473be8f8d';volume_id='vol-0cc9bf3a79eaa3c07'
ec2=boto3.client('ec2',region_name='us-east-1')
i=ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]
assert i['InstanceType']=='m7i.4xlarge'
assert {t['Key']:t['Value'] for t in i['Tags']}['Experiment']=='literature-ablation-kimi-continuation-20261106'
assert len(i['BlockDeviceMappings'])==1
assert i['BlockDeviceMappings'][0]['Ebs']['VolumeId']==volume_id
assert i['BlockDeviceMappings'][0]['Ebs']['DeleteOnTermination']
shared=ec2.describe_instances(Filters=[{'Name':'tag:Name','Values':['g6-big2']},{'Name':'instance-state-name','Values':['running']}])
shared_ids=[x['InstanceId'] for r in shared['Reservations'] for x in r['Instances']]
assert instance_id not in shared_ids
record={'experiment_id':audit['experiment_id'],'instance_id':instance_id,'volume_id':volume_id,'authorized_budget_usd':50,'region':'us-east-1','verification_record':publication['records']['final-backup-verification.json'],'requested_at':datetime.now(timezone.utc).isoformat(),'initial_launch_time':'2026-11-06T07:49:57+00:00','hourly_instance_price_usd':0.8064,'preexisting_shared_running_instances':shared_ids}
(out/'resource-cleanup-request.json').write_text(json.dumps(record,indent=2)+'\n')
accounting=json.loads((out/'provider-accounting.json').read_text());assert accounting['all_disabled']
record['reported_model_spend_usd']=accounting['total_model_spend_usd']
assert (datetime.now(timezone.utc)-datetime.fromisoformat(record['initial_launch_time'])).total_seconds()/3600*0.8064+record['reported_model_spend_usd'] < 48
ec2.terminate_instances(InstanceIds=[instance_id])
for _ in range(120):
 state=ec2.describe_instances(InstanceIds=[instance_id])['Reservations'][0]['Instances'][0]['State']['Name']
 if state=='terminated':break
 time.sleep(3)
else:raise TimeoutError('Dedicated instance termination not confirmed')
for _ in range(30):
 try: ec2.describe_volumes(VolumeIds=[volume_id])
 except ClientError as error:
  if error.response['Error']['Code']!='InvalidVolume.NotFound':raise
  break
 time.sleep(3)
else:raise TimeoutError('DeleteOnTermination volume removal not confirmed')
for shared_id in shared_ids:
 assert ec2.describe_instances(InstanceIds=[shared_id])['Reservations'][0]['Instances'][0]['State']['Name']=='running', shared_id
end=datetime.now(timezone.utc)
record.update(instance_state='terminated',volume_deleted=True,confirmed_at=end.isoformat(),shared_instances_unchanged=True,credential_copy_removed_with_deleted_volume=True)
record['wall_hours_upper_bound']=(end-datetime.fromisoformat(record['initial_launch_time'])).total_seconds()/3600
record['estimated_instance_cost_upper_bound_usd']=record['wall_hours_upper_bound']*record['hourly_instance_price_usd']
record['cost_note']='Conservative elapsed-time VM estimate includes the brief stopped interval; not a settled bill. Storage, requests and transfers are additional within the approved $50 cap.'
record['model_plus_compute_upper_bound_usd']=record['reported_model_spend_usd']+record['estimated_instance_cost_upper_bound_usd']
(out/'resource-cleanup.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record,indent=2))
