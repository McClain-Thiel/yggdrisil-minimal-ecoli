"""Provision exactly one explicitly approved baseline-validation worker."""
import fcntl
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

OP = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/vecoli-deletion-baselines-20gen-3seed-20261106')
spec = json.loads((OP / 'protocol.json').read_bytes())
with (OP / 'provision.lock').open('a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (OP / 'worker-launch.json').exists():
        raise RuntimeError('Worker already recorded; inspect instead of provisioning again')
    ec2 = boto3.client('ec2', region_name='us-east-1')
    previous = ec2.describe_instances(Filters=[
        {'Name': 'tag:Experiment', 'Values': [spec['experiment_id']]},
        {'Name': 'instance-state-name', 'Values': ['pending', 'running', 'stopping', 'stopped']}])
    if any(r['Instances'] for r in previous['Reservations']):
        raise RuntimeError('Existing experiment worker found; no duplicate allowed')
    filters = {'instanceType': 'm7i.4xlarge', 'location': 'US East (N. Virginia)',
               'operatingSystem': 'Linux', 'tenancy': 'Shared', 'preInstalledSw': 'NA',
               'capacitystatus': 'Used'}
    products = boto3.client('pricing', region_name='us-east-1').get_products(
        ServiceCode='AmazonEC2', Filters=[{'Type': 'TERM_MATCH', 'Field': k, 'Value': v}
                                        for k, v in filters.items()])
    charges = [float(d['pricePerUnit']['USD']) for p in products['PriceList']
               for t in json.loads(p)['terms']['OnDemand'].values()
               for d in t['priceDimensions'].values() if d['unit'] == 'Hrs']
    if charges != [0.8064]:
        raise RuntimeError('Price changed from approved proposal')
    now = datetime.now(timezone.utc)
    stop, science = now + timedelta(hours=24), now + timedelta(hours=23)
    auth = {'experiment_id': spec['experiment_id'], 'paid_launch': True,
            'approved_at': now.isoformat(), 'total_cap_usd': 30,
            'user_authorization': 'Approve up to $30',
            'scope': '10 frozen baseline genomes plus two controls, three seeds, 20 generations, one16vCPU worker;24h hard stop;W&B-only credential transfer over encrypted SSH;verified S3/W&B;monitoring and cleanup.',
            'model_calls_allowed': False, 'credential_transfer_approved': True,
            'protocol_sha256': hashlib.sha256((OP / 'protocol.json').read_bytes()).hexdigest(),
            'manifest_sha256': hashlib.sha256((OP / 'manifest.json').read_bytes()).hexdigest(),
            'solver_policy_scope': 'The unchanged v2 patch applies uniformly to ALL TWELVE genotypes. Prior protocol text saying five refers to the previous study; no scientific change.',
            'science_deadline_unix': science.timestamp(), 'stop_deadline_unix': stop.timestamp(),
            'science_deadline': science.isoformat(), 'stop_deadline': stop.isoformat(),
            'hourly_usd': charges[0], 'max_compute_usd': 24 * charges[0]}
    base = spec['execution']['remote_root']
    user_data = '''#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/yggdrisil-validation-bootstrap.log) 2>&1
cat > /etc/systemd/system/yggdrisil-baseline-validation-stop.service <<'SERVICE'
[Unit]
Description=Approved dedicated validation budget stop
[Service]
Type=oneshot
ExecStart=/usr/sbin/shutdown -h now
SERVICE
cat > /etc/systemd/system/yggdrisil-baseline-validation-stop.timer <<'TIMER'
[Unit]
Description=Absolute validation spending deadline
[Timer]
OnCalendar=STOP_DATE
Persistent=true
Unit=yggdrisil-baseline-validation-stop.service
[Install]
WantedBy=timers.target
TIMER
systemctl daemon-reload
systemctl enable --now yggdrisil-baseline-validation-stop.timer
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y build-essential git curl unzip openjdk-21-jre-headless pkg-config libopenblas-dev liblapack-dev libglpk-dev python3-venv python3-dev
install -d -o ubuntu -g ubuntu ROOT_DIR
sudo -u ubuntu python3 -m venv ROOT_DIR/env
sudo -u ubuntu ROOT_DIR/env/bin/pip install uv==0.11.31
sudo -u ubuntu bash -c 'set -euo pipefail; cd ROOT_DIR; git clone https://github.com/CovertLab/vEcoli.git native; cd native; git checkout b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc; ../env/bin/uv python install 3.12.12; ../env/bin/uv sync --frozen --no-dev --python 3.12.12'
install -d -o ubuntu -g ubuntu ROOT_DIR/bin
sudo -u ubuntu bash -c 'set -euo pipefail; cd ROOT_DIR/bin; curl -fsSL https://github.com/nextflow-io/nextflow/releases/download/v25.10.4/nextflow -o nextflow; chmod 755 nextflow; NXF_VER=25.10.4 ./nextflow -version'
touch ROOT_DIR/bootstrap.ready
'''.replace('STOP_DATE', stop.strftime('%Y-%m-%d %H:%M:%S UTC')).replace('ROOT_DIR', base)
    (OP / 'source/cloud-init.sh').write_text(user_data)
    (OP / 'approval.json').write_text(json.dumps(auth, indent=2) + '\n')
    images = ec2.describe_images(ImageIds=['ami-0d6dd9e8265d48906'])['Images']
    if len(images) != 1 or images[0]['State'] != 'available' or images[0]['Architecture'] != 'x86_64':
        raise RuntimeError('Proposed Ubuntu image unavailable')
    response = ec2.run_instances(ImageId=images[0]['ImageId'], InstanceType='m7i.4xlarge',
        MinCount=1, MaxCount=1, KeyName='mcclain-phd-aws-dev',
        SecurityGroupIds=['sg-0428643a8cce57e60'], SubnetId='subnet-0dae315348972fe74',
        IamInstanceProfile={'Name': 's3-access'},
        BlockDeviceMappings=[{'DeviceName': '/dev/sda1', 'Ebs': {
            'VolumeSize': 200, 'VolumeType': 'gp3', 'Encrypted': True, 'DeleteOnTermination': True}}],
        MetadataOptions={'HttpTokens': 'required'}, InstanceInitiatedShutdownBehavior='stop',
        TagSpecifications=[{'ResourceType': kind, 'Tags': [
            {'Key': 'Name', 'Value': spec['experiment_id']},
            {'Key': 'Experiment', 'Value': spec['experiment_id']}, {'Key': 'BudgetUSD', 'Value': '30'}]}
                           for kind in ['instance', 'volume']],
        ClientToken='yggdrisil-vecoli-baselines-20261106-approved30', UserData=user_data)
    instance = response['Instances'][0]
    auth['instance_id'] = instance['InstanceId']
    (OP / 'approval.json').write_text(json.dumps(auth, indent=2) + '\n')
    record = {'instance_id': instance['InstanceId'], 'region': 'us-east-1',
              'status': instance['State']['Name'], 'instance_type': instance['InstanceType'],
              'launch_time': instance['LaunchTime'].isoformat(), 'public_ip': None,
              'remote_root': base, 'approval_cap_usd': 30,
              'science_deadline': science.isoformat(), 'stop_deadline': stop.isoformat(),
              'hourly_usd': charges[0], 'max_parallel_cells': 9}
    (OP / 'worker-launch.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))
