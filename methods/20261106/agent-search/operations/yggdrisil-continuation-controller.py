"""Supervise only the three authorized remote search continuations."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
import boto3
import wandb

ROOT=Path(__file__).resolve().parent
PLAN=json.loads((ROOT/'protocol.json').read_text())
BUNDLE=ROOT/'application/runs'/PLAN['experiment_id']
PYTHON=ROOT/'env/bin/python'
lock=(ROOT/'search-controller.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
if (ROOT/'search-progress.json').exists():raise RuntimeError('Already launched; inspect before recovery')
assert sys.platform=='linux'
assert PLAN['authorization']['paid_launch'] and PLAN['authorization']['incremental_cap_usd']==50
assert [a['model_budget_usd'] for a in PLAN['arms']]==[12,12,12]
common=json.loads((ROOT/'credentials/common.json').read_text());os.environ.update(common)
secrets=[v.encode() for p in (ROOT/'credentials').glob('*.json') for k,v in json.loads(p.read_text()).items() if k.endswith('_KEY') and isinstance(v,str)]
assert len(secrets)==4
S3=boto3.client('s3',region_name='us-east-1');location=urlparse(PLAN['tracking']['s3_prefix'])
progress={'experiment_id':PLAN['experiment_id'],'status':'backing_up_inputs','started_at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid(),'jobs':{}}
children={};logs=[]
def save(path,data):
 temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2)+'\n');temp.replace(path)
def record():
 progress['updated_at']=datetime.now(timezone.utc).isoformat();save(ROOT/'search-progress.json',progress)
def sha(path):
 with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def upload(path,category):
 digest=sha(path);key=location.path.strip('/')+'/'+category+'/'+digest+(''.join(path.suffixes))
 S3.upload_file(str(path),location.netloc,key,ExtraArgs={'ServerSideEncryption':'AES256','Metadata':{'sha256':digest}})
 h=hashlib.sha256()
 with S3.get_object(Bucket=location.netloc,Key=key)['Body'] as f:
  while chunk := f.read(1024**2):h.update(chunk)
 if h.hexdigest()!=digest:raise RuntimeError('S3 round-trip verification failed')
 return {'uri':f's3://{location.netloc}/{key}','sha256':digest,'bytes':path.stat().st_size}
def checkpoint(tracked):
 stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');stage=ROOT/'checkpoint-staging'/stamp;stage.mkdir(parents=True)
 sources=[ROOT/n for n in ['protocol.json','source-snapshot.json','search-progress.json','preflight.json','instance.json'] if (ROOT/n).exists()]
 sources+=list(ROOT.glob('search-*.log'))
 sources+=[p for p in (ROOT/'attempts').rglob('*') if p.is_file() and p.suffix in {'.json','.log'}]
 sources+=list(BUNDLE.glob('*.json'))+list((BUNDLE/'inputs').glob('*.json'))
 for base in [BUNDLE/'parents',BUNDLE/'search']:
  sources += [p for p in base.rglob('*') if p.is_file() and p.name in {'graph.sqlite','agents.sqlite','config.json','launch.json','progress.json','result.json','tracking.json','artifact.json','failure.json','parent-checkpoint.json'}]
 inventory=[]
 for source in sorted(set(sources)):
  target=stage/source.relative_to(ROOT);target.parent.mkdir(parents=True,exist_ok=True)
  if source.suffix=='.sqlite':
   with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:
    src.backup(dst)
    if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise RuntimeError('Invalid SQLite backup')
  else:shutil.copyfile(source,target)
  data=target.read_bytes()
  if any(s in data for s in secrets) or re.search(rb'sk-or-v1-[a-fA-F0-9]{32,}|(?:AKIA|ASIA)[A-Z0-9]{16}',data):raise RuntimeError('Credential detected in checkpoint')
  inventory.append({'path':str(target.relative_to(stage)),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
 save(stage/'checkpoint.json',{'files':inventory,'created_at':stamp,'sqlite_consistency':'Individually consistent SQLite backups; sequential across databases','excluded':'Source and credentials'})
 archive=stage.with_suffix('.tar.gz')
 with tarfile.open(archive,'w:gz') as tar:
  for path in stage.rglob('*'):
   if path.is_file():tar.add(path,arcname=str(path.relative_to(stage)),recursive=False)
 ref=upload(archive,'checkpoints')
 artifact=wandb.Artifact(PLAN['experiment_id']+'-checkpoint',type='experiment-checkpoint',metadata=ref);artifact.add_file(str(archive),name='checkpoint.tar.gz');tracked.log_artifact(artifact).wait()
 cloud=wandb.Api().artifact(artifact.qualified_name)
 if cloud.state!='COMMITTED':raise RuntimeError('W&B checkpoint not committed')
 downloaded=Path(cloud.get_entry('checkpoint.tar.gz').download(root=str(ROOT/'verification'/stamp)))
 if sha(downloaded)!=ref['sha256']:raise RuntimeError('W&B round-trip verification failed')
 ref.update(wandb_artifact=artifact.qualified_name,verified_at=datetime.now(timezone.utc).isoformat(),files=len(inventory))
 save(ROOT/'latest-search-checkpoint.json',ref);progress['latest_checkpoint']=ref;record()
 print('Verified checkpoint',artifact.qualified_name,flush=True)
 return ref

def stop():
 for p in children.values():
  if p.poll() is None:os.killpg(p.pid,signal.SIGINT)
 for p in children.values():
  try:p.wait(timeout=60)
  except subprocess.TimeoutExpired:
   os.killpg(p.pid,signal.SIGKILL);p.wait()

with wandb.init(entity='mcclain',project='yggdrisil',name=PLAN['experiment_id']+'-checkpoints',tags=['yggdrisil','ablation','continuation','preservation'],dir=str(ROOT),config={'model_cap_usd':36,'total_cap_usd':50},settings=wandb.Settings(disable_git=True,disable_code=True,save_code=False)) as tracked:
 progress['wandb_url']=tracked.url;record()
 try:
  checkpoint(tracked)
  for index,arm in enumerate(PLAN['arms']):
   env=os.environ.copy();env.update(json.loads((ROOT/f'credentials/arm-{index}.json').read_text()));env.update(PYTHONPATH=f'{ROOT}/framework/src:{ROOT}/application/src:{ROOT}/application',PYTHONUNBUFFERED='1',OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
   log=(ROOT/f'search-{index}.log').open('xb');logs.append(log)
   cmd=[str(PYTHON),'-m','scripts.run_literature_ablation','--bundle',str(BUNDLE),'--job-index',str(index),'--repo-root',str(ROOT/'application'),'--model-budget-usd','12']
   if (BUNDLE/'search'/arm['execution_id']/'config.json').exists():cmd.append('--resume')
   p=subprocess.Popen(cmd,cwd=ROOT/'application',env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);children[index]=p
   progress['jobs'][str(index)]={'pid':p.pid,'arm':arm['arm'],'execution_id':arm['execution_id'],'status':'running'}
  progress['status']='running';record();last=0
  while any(p.poll() is None for p in children.values()):
   if time.time()>json.loads((ROOT/'deadlines.json').read_text())['science_unix']:raise TimeoutError('Authorized scientific deadline reached')
   if shutil.disk_usage(ROOT).free<5*1024**3:raise RuntimeError('Low scratch space; preserve data')
   for index,p in children.items():
    progress['jobs'][str(index)]['exit_code']=p.poll()
   if time.monotonic()-last>=120:checkpoint(tracked);last=time.monotonic()
   time.sleep(10)
  failed=[]
  for index,p in children.items():
   progress['jobs'][str(index)].update(exit_code=p.returncode,status='completed' if p.returncode==0 else 'failed')
   if p.returncode:failed.append(index)
  progress['status']='completed' if not failed else 'completed_with_exceptions';progress['failed_arms']=failed
 except BaseException as exc:
  progress['status']='failed';progress['error']=repr(exc);print('Controller failed:',repr(exc),flush=True);raise
 finally:
  stop()
  for log in logs:log.close()
  record();checkpoint(tracked)
  progress['all_model_children_stopped']=True;record()
  # The local monitor disables the three dedicated keys; management stays local.
  save(ROOT/'search-ended.json',progress)
