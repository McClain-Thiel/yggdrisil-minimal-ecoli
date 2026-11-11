import hashlib,json,time
from pathlib import Path
from datetime import datetime,timezone,timedelta
import boto3

op=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/deletion-baselines-20261106')
read=lambda p:json.loads(p.read_bytes())
spec=read(op/'protocol.json');deploy=read(op/'deployment.json')
ec2=boto3.client('ec2',region_name='us-east-1');prices=boto3.client('pricing',region_name='us-east-1')
if (op/'worker-launch.json').exists():raise RuntimeError('Worker already recorded; inspect before any resume')
prior=ec2.describe_instances(Filters=[{'Name':'tag:Experiment','Values':[spec['experiment_id']]},{'Name':'instance-state-name','Values':['pending','running','stopping','stopped']}])
assert not any(r['Instances'] for r in prior['Reservations']), 'Existing study worker found'
filters=[{'Type':'TERM_MATCH','Field':k,'Value':v} for k,v in {'instanceType':'m7i.4xlarge','location':'US East (N. Virginia)','operatingSystem':'Linux','tenancy':'Shared','preInstalledSw':'NA','capacitystatus':'Used'}.items()]
products=prices.get_products(ServiceCode='AmazonEC2',Filters=filters,MaxResults=100)
charges=[]
for p in products['PriceList']:
 d=json.loads(p)
 for t in d.get('terms',{}).get('OnDemand',{}).values():
  for price in t['priceDimensions'].values():
   if price['unit']=='Hrs':charges.append(float(price['pricePerUnit']['USD']))
assert charges and all(p==0.8064 for p in charges),charges
(op/'launch-pricing.json').write_text(json.dumps({'checked_at':datetime.now(timezone.utc).isoformat(),'hourly_usd':charges[0],'compute24h_usd':24*charges[0],'total_cap_usd':25},indent=2)+'\n')
now=datetime.now(timezone.utc);stop=now+timedelta(hours=24);science=stop-timedelta(hours=1)
unit=f'''[Unit]\nDescription=Baseline absolute approved budget stop\n[Timer]\nOnCalendar={stop.strftime('%Y-%m-%d %H:%M:%S UTC')}\nPersistent=true\nUnit=yggdrisil-baseline-budget-stop.service\n[Install]\nWantedBy=timers.target\n'''
service='[Unit]\nDescription=Stop dedicated baseline worker at approved cap\n[Service]\nType=oneshot\nExecStart=/usr/sbin/shutdown -h now\n'
user_data='''#!/bin/bash
set -euo pipefail
install -d -m 755 /etc/systemd/system
cat > /etc/systemd/system/yggdrisil-baseline-budget-stop.timer <<'TIMER'
'''+unit+'''TIMER
cat > /etc/systemd/system/yggdrisil-baseline-budget-stop.service <<'SERVICE'
'''+service+'''SERVICE
systemctl daemon-reload
systemctl enable --now yggdrisil-baseline-budget-stop.timer
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-dev build-essential libglpk40
sudo -u ubuntu python3 -m venv /home/ubuntu/yggdrisil-baselines-20261106/env
sudo -u ubuntu /home/ubuntu/yggdrisil-baselines-20261106/env/bin/python -m pip install uv==0.11.31
'''
(op/'source/operations').mkdir(exist_ok=True)
(op/'source/operations/cloud-init.sh').write_text(user_data)
auth={**deploy['approval_template'],'paid_launch':True,'model_calls_allowed':False,'user_authorization':'Approve up to $25; ten non-LLM baseline searches on one16vCPU AWS worker,24h hard stop, verified S3/W&B; only W&B tracking key over encrypted SSH.','approved_at':now.isoformat(),'total_cap_usd':25,'science_deadline_unix':science.timestamp(),'stop_deadline_unix':stop.timestamp(),'region':'us-east-1'}
assert hashlib.sha256((op/'protocol.json').read_bytes()).hexdigest()==auth['protocol_sha256']
assert hashlib.sha256((op/'manifest.json').read_bytes()).hexdigest()==auth['bundle_manifest_sha256']
(op/'approval.json').write_text(json.dumps(auth,indent=2)+'\n')
response=ec2.run_instances(ImageId='ami-0d6dd9e8265d48906',InstanceType='m7i.4xlarge',MinCount=1,MaxCount=1,KeyName='mcclain-phd-aws-dev',SecurityGroupIds=['sg-0428643a8cce57e60'],SubnetId='subnet-0dae315348972fe74',IamInstanceProfile={'Name':'s3-access'},BlockDeviceMappings=[{'DeviceName':'/dev/sda1','Ebs':{'VolumeSize':200,'VolumeType':'gp3','Encrypted':True,'DeleteOnTermination':True}}],MetadataOptions={'HttpTokens':'required'},TagSpecifications=[{'ResourceType':'instance','Tags':[{'Key':'Name','Value':spec['experiment_id']},{'Key':'Experiment','Value':spec['experiment_id']},{'Key':'BudgetUSD','Value':'25'}]}],ClientToken='yggdrisil-deletion-baselines-20261106-approved25',UserData=user_data,InstanceInitiatedShutdownBehavior='stop')
i=response['Instances'][0];auth['instance_id']=i['InstanceId'];(op/'approval.json').write_text(json.dumps(auth,indent=2)+'\n')
record={'instance_id':i['InstanceId'],'region':'us-east-1','status':i['State']['Name'],'launch_time':i['LaunchTime'].isoformat(),'science_deadline':science.isoformat(),'stop_deadline':stop.isoformat(),'hourly_usd':0.8064,'approved_cap_usd':25,'max_concurrent_jobs':4,'source_bundle_sha256':auth['bundle_manifest_sha256']}
(op/'worker-launch.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
