import json
import time
from datetime import datetime, timezone
from pathlib import Path
import boto3
from botocore.exceptions import ClientError

OUT=Path('/private/tmp/yggdrisil-baseline-final-audit')
FINAL=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/vecoli-deletion-baselines-20gen-3seed-20261106/final')
read=lambda p:json.loads(p.read_bytes())
now=lambda:datetime.now(timezone.utc).isoformat()
instance='i-0e673348d39a6b805'
volume='vol-023bdfb958cf0ad57'
shared='i-0cda00ffb3cacfc12'
study='vecoli-deletion-baselines-20gen-3seed-20261106'
publication=read(FINAL/'publication.json')
assert publication['status']=='verified_cleanup_pending'
assert publication['full_configured_secret_scan'] and publication['full_s3_round_trip_verified']
assert publication['analysis_artifact']['state']=='COMMITTED'
assert publication['analysis_artifact']['all_files_download_sha256_verified']
assert read(OUT/'artifact-verification.json')['status']=='verified'
assert read(OUT/'checkpoint-archive-comparison.json')['status']=='verified'
assert read(OUT/'archive-member-audit.json')['status']=='verified'
ec2=boto3.client('ec2',region_name='us-east-1')
def describe(i):
    return ec2.describe_instances(InstanceIds=[i])['Reservations'][0]['Instances'][0]
before=describe(instance)
assert before['State']['Name']=='stopped', 'Expected the completed dedicated worker to be stopped'
tags={t['Key']:t['Value'] for t in before['Tags']}
assert tags['Experiment']==study and tags['BudgetUSD']=='30'
devices=before['BlockDeviceMappings']
assert len(devices)==1 and devices[0]['Ebs']['VolumeId']==volume and devices[0]['Ebs']['DeleteOnTermination']
disk=ec2.describe_volumes(VolumeIds=[volume])['Volumes'][0]
assert disk['Encrypted'] and disk['Size']==200
assert disk['Attachments'][0]['InstanceId']==instance
assert describe(shared)['State']['Name']=='running'
record={'instance_id':instance,'volume_id':volume,'experiment_id':study,'requested_at':now(),
        'pre_cleanup_state':before['State']['Name'],'delete_on_termination':True,'volume_encrypted':True,
        'verified_data_receipt':read(FINAL/'publication-receipt.json'),'status':'termination_requested'}
record['termination_response']=ec2.terminate_instances(InstanceIds=[instance])['TerminatingInstances']
(OUT/'worker-cleanup.json').write_text(json.dumps(record,indent=2)+'\n')
for attempt in range(60):
    state=describe(instance)['State']['Name']
    if state=='terminated':
        break
    time.sleep(10)
else:
    raise RuntimeError('Dedicated worker did not reach terminated within ten minutes')
for attempt in range(60):
    try:
        disks=ec2.describe_volumes(VolumeIds=[volume])['Volumes']
    except ClientError as error:
        if error.response['Error']['Code']!='InvalidVolume.NotFound':
            raise
        print('DeleteOnTermination volume absence verified',flush=True)
        break
    if not disks:
        break
    time.sleep(5)
else:
    raise RuntimeError('Dedicated volume still exists after termination')
assert describe(shared)['State']['Name']=='running'
launch=datetime.fromisoformat('2026-11-06T17:19:41+00:00')
observed_stop=datetime.fromisoformat(read(OUT/'worker-before-cleanup.json')['checked_at'])
compute_upper=(observed_stop-launch).total_seconds()/3600*0.8064
record.update(status='terminated_and_volume_deleted',verified_at=now(),shared_worker_id=shared,shared_worker_state='running',
    scientific_deadline_unchanged='2026-11-07T16:19:39.785015+00:00',absolute_deadline_unchanged='2026-11-07T17:19:39.785015+00:00',
    cost={'approved_total_cap_usd':30,'instance_hourly_usd':0.8064,'compute_upper_bound_usd':compute_upper,
          'bound_basis':'Conservatively counts the entire interval from original launch until independently observed stopped, including shutdown/restart gaps and stopped time. This is not an AWS bill. Storage and transfer charges are separate.',
          'first_launch':launch.isoformat(),'observed_stopped_at':observed_stop.isoformat()})
(OUT/'worker-cleanup.json').write_text(json.dumps(record,indent=2)+'\n')
print(json.dumps(record),flush=True)
