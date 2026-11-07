"""Retire only the dedicated baseline worker after independently verified backups."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

root = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/deletion-baselines-20261106')
read = lambda p: json.loads(p.read_bytes())
out = root / 'final'
audit = read(out / 'audit.json')
analysis = read(out / 'analysis.json')
publication = read(out / 'completion-record-publication.json')
approval = read(root / 'approval.json')
assert audit['status'] == 'independently_verified'
assert audit['all_member_names_sizes_sha256_verified'] and audit['worker_files_verified']
assert len(audit['sqlite_logical_comparisons']) == 10 and len(audit['artifacts']) == 11
assert analysis['jobs_completed'] == 10 and analysis['new_evaluated_states_total'] == 5000
assert publication['status'] == 'verified' and publication['wandb_download_verified'] and publication['full_s3_get_verified']
for name in ['analysis', 'audit', 'nominations']:
    assert hashlib.sha256((out / (name + '.json')).read_bytes()).hexdigest() == publication[name + '_sha256']
instance_id = 'i-0357352c1f7c337d2'
volume_id = 'vol-0eeab643d0e3b6c01'
shared_id = 'i-0cda00ffb3cacfc12'
assert approval['instance_id'] == instance_id and approval['total_cap_usd'] == 25
ec2 = boto3.client('ec2', region_name='us-east-1')
def instance(identity):
    return ec2.describe_instances(InstanceIds=[identity])['Reservations'][0]['Instances'][0]
before = instance(instance_id)
assert before['InstanceType'] == 'm7i.4xlarge'
volumes = [b['Ebs'] for b in before['BlockDeviceMappings']]
assert len(volumes) == 1 and volumes[0]['VolumeId'] == volume_id and volumes[0]['DeleteOnTermination']
record = {'requested_at': datetime.now(timezone.utc).isoformat(),
    'instance_id': instance_id, 'volume_id': volume_id, 'status': 'termination_requested',
    'verified_final_archive': audit['final_backup'], 'verified_completion_records': publication,
    'shared_instance_id': shared_id, 'shared_state_before': instance(shared_id)['State']['Name'],
    'approval_cap_usd': 25, 'new_model_calls': 0}
(out / 'cleanup-request.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
if before['State']['Name'] not in ['terminated', 'shutting-down']:
    ec2.terminate_instances(InstanceIds=[instance_id])
for _ in range(24):
    current = instance(instance_id)
    if current['State']['Name'] == 'terminated':
        break
    time.sleep(5)
else:
    raise RuntimeError('Termination still pending; preserve cleanup-request.json and inspect before retrying')
for _ in range(12):
    try:
        ec2.describe_volumes(VolumeIds=[volume_id])
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'InvalidVolume.NotFound':
            raise
        break
    time.sleep(5)
else:
    raise RuntimeError('Instance terminated but dedicated volume still exists; do not report cleanup complete')
now = datetime.now(timezone.utc)
launch = datetime.fromisoformat(read(root / 'worker-launch.json')['launch_time'])
record.update(status='terminated_volume_deleted', verified_at=now.isoformat(),
    shared_state_after=instance(shared_id)['State']['Name'], shared_instance_modified=False,
    compute_cost_upper_bound_usd=(now - launch).total_seconds() / 3600 * 0.8064,
    cost_method='Conservative uninterrupted instance time from original launch through termination; includes stopped periods. Storage/requests/transfers excluded; not a settled AWS bill.')
(out / 'cleanup.json').write_text(json.dumps(record, indent=2, sort_keys=True) + '\n')
print(json.dumps({k: record[k] for k in ['status', 'instance_id', 'volume_id', 'shared_state_after', 'compute_cost_upper_bound_usd']}))
