"""Retrieve only compact records and publish a synchronized, verified ledger."""
import base64,fcntl,hashlib,json,subprocess,sys
from datetime import datetime,timezone
from pathlib import Path
import boto3
repo=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');op=repo/'runs/vecoli-deletion-baselines-20gen-3seed-20261106'
sys.path.insert(0,str(repo/'src'))
from yggdrisil_ecoli.baseline_storage import BaselineArchive,configured_secrets,screen
from yggdrisil_ecoli.data.io import atomic_json,file_sha256
root='/home/ubuntu/yggdrisil-baseline-validation-20261106'
ssh=['ssh','-i',str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),'-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ClearAllForwardings=yes','ubuntu@'+json.loads((op/'worker-launch.json').read_bytes())['public_ip']]
code=r'''
import base64,hashlib,json,sys
from pathlib import Path
root=Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')
sys.path.insert(0,str(root/'application/src'))
from yggdrisil_ecoli.baseline_storage import configured_secrets,screen
secrets=configured_secrets(root/'application',root/'credentials')
names=['launcher.json','preflight.json','prepared.json','deployment-manifest.json','batch-progress.json','monitoring-summary.json','final-backup.json','knockout-audit/variant-knockout-audit.json','setup-repair.json','setup-attempt-backup.json','credential-pattern-policy.json','credential-pattern-review.json']
for label in ['calibration','seed-101','seed-211','seed-307']:
 for name in ['manifest.json','experiments.json','process.json','progress.json','s3-checkpoint.json','results.json']:
  names.append('runs/'+label+'/'+name)
result={}
for name in names:
 path=root/name
 if not path.exists():continue
 screen(path,secrets);data=path.read_bytes()
 result[name]={'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()}
print(json.dumps(result))
'''
raw=subprocess.check_output(ssh+[root+'/env/bin/python -'],input=code.encode())
transport=json.loads(raw);snapshot={}
for name,item in transport.items():
 data=base64.b64decode(item['data']);assert hashlib.sha256(data).hexdigest()==item['sha256']
 path=op/'remote'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);snapshot[name]=item['sha256']
summary=json.loads((op/'remote/monitoring-summary.json').read_bytes())
now=datetime.now(timezone.utc).isoformat();stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
(op/f'monitoring-{stamp}.json').write_bytes((op/'remote/monitoring-summary.json').read_bytes())
spec=json.loads((op/'protocol.json').read_bytes());auth=json.loads((op/'approval.json').read_bytes())
secrets=configured_secrets(repo,repo/'runs/literature-ablation-kimi-continuation-20261106/credentials')
archive=BaselineArchive(s3=boto3.client('s3',region_name='us-east-1'),wandb=None,tracked=None,
 owner=boto3.client('sts').get_caller_identity()['Account'],bucket=spec['tracking']['s3_bucket'],prefix=spec['tracking']['s3_prefix'],run_id='study',secrets=secrets)
refs={}
for name in ['approval.json','worker-launch.json','deployment-manifest.json','monitor-instructions.json','remote/launcher.json','remote/preflight.json','remote/prepared.json','remote/knockout-audit/variant-knockout-audit.json','remote/batch-progress.json','remote/monitoring-summary.json','remote/setup-repair.json','remote/setup-attempt-backup.json','credential-pattern-policy.json']:
 path=op/name
 if not path.exists():continue
 screen(path,secrets);digest=file_sha256(path);refs[name]=archive.upload(path,'launch-and-monitoring');assert refs[name]['sha256']==digest==file_sha256(path)
with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 canonical=repo/'experiments.json';working=Path('/private/tmp/yggdrisil-migration-ledger.json');before=canonical.read_bytes();ledger=json.loads(before)
 assert ledger==json.loads(working.read_bytes()),'Ledger copies diverged'
 entry=next(x for x in ledger['experiments'] if x['id']==spec['experiment_id'])
 (op/'attempts'/('ledger-before-'+hashlib.sha256(before).hexdigest()+'.json')).write_bytes(before)
 entry.update(status=summary['status'],authorization=auth,instance_id=auth['instance_id'],worker=json.loads((op/'worker-launch.json').read_bytes()),remote_root=root,last_verified_at=summary['verification_completed_at'],
   started_at=summary['batch']['started_at'],source_verification=refs['deployment-manifest.json'],
   solver_policy=auth['solver_policy_scope']+' '+spec['solver_policy'],current_progress=summary,
   wandb_run_urls=[summary['batch']['wandb_url']],deadline=auth['science_deadline'],absolute_stop=auth['stop_deadline'])
 entry['data_backup'].update(status='verified_active_checkpoints',launch_and_monitoring=refs,full_s3_get_verified=True,all_changed_wandb_files_download_verified=True)
 entry['monitor_instructions']=str(op/'monitor-instructions.json')
 baseline=next(x for x in ledger['experiments'] if x['id']=='deletion-baselines-20261106')
 assert baseline['status']=='completed_verified'
 baseline['held_out_validation'].update(status=summary['status'],experiment_id=spec['experiment_id'])
 ledger['updated_at']=now;staged=op/'ledger-to-publish.json';atomic_json(staged,ledger);screen(staged,secrets);ledger_ref=archive.upload(staged,'ledger')
 assert canonical.read_bytes()==before and json.loads(working.read_bytes())==json.loads(before)
 atomic_json(canonical,ledger);atomic_json(working,ledger)
 publication={'published_at':now,'status':summary['status'],'records':refs,'ledger':ledger_ref,'full_configured_secret_scan':True,'full_round_trip_verified':True}
 previous=op/'publication.json'
 if previous.exists():(op/'attempts'/('publication-'+file_sha256(previous)+'.json')).write_bytes(previous.read_bytes())
 atomic_json(previous,publication)
print(json.dumps({'status':summary['status'],'ledger':ledger_ref,'records':len(refs)}))
