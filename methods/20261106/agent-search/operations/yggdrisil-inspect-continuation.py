"""Read current remote state and independently verify immutable checkpoints."""
import fcntl,hashlib,json,os,shutil,subprocess,tempfile
from pathlib import Path
from datetime import datetime,timezone
from urllib.parse import urlparse
import boto3,wandb
r=Path('/home/ubuntu/yggdrisil-continuation-20261106')
os.environ.update(json.loads((r/'credentials/common.json').read_text()))
secrets=[v.encode() for p in (r/'credentials').glob('*.json') for k,v in json.loads(p.read_text()).items() if k.endswith('_KEY') and isinstance(v,str)]
s3=boto3.client('s3',region_name='us-east-1');api=wandb.Api(timeout=60)
def read(p):return json.loads(p.read_text())
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def remote(ref):
 loc=urlparse(ref['uri']);body=s3.get_object(Bucket=loc.netloc,Key=loc.path.lstrip('/'))['Body']
 try:data=body.read()
 finally:body.close()
 if hashlib.sha256(data).hexdigest()!=ref['sha256'] or ('bytes' in ref and len(data)!=ref['bytes']):raise RuntimeError('S3 checksum mismatch: '+ref['uri'])
 return data
def process(p):
 if not p.exists():return None
 x=read(p);pid=x.get('pid',x.get('controller_pid'));stat=Path(f'/proc/{pid}/stat')
 x['alive']=stat.exists() and stat.read_text().split()[2]!='Z'
 if x['alive'] and 'start_ticks' in x:x['identity_matches']=stat.read_text().split()[21]==x['start_ticks']
 return x
def lock_status(p):
 if not p.exists():return 'not_created'
 with p.open() as f:
  try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError:return 'held'
  fcntl.flock(f,fcntl.LOCK_UN);return 'free'
summary={'checked_at':datetime.now(timezone.utc).isoformat(),'instance_id':'i-0512356f473be8f8d','search':read(r/'search-progress.json'),'search_launcher':process(r/'search-launcher.json'),'search_lock':lock_status(r/'search-controller.lock'),'arms':{}}
for p in [r/'source-snapshot.json',r/'validation-readiness.json']:
 if p.exists():
  for name,expected in read(p)['files'].items():
   if sha(r/name)!=expected:raise RuntimeError('Recorded code/metadata hash changed: '+name)
summary['source_files_verified']=True
ref=summary['search'].get('latest_checkpoint')
if ref:
 data=remote(ref);artifact=api.artifact(ref['wandb_artifact']);assert artifact.state=='COMMITTED'
 with tempfile.TemporaryDirectory(prefix='monitor-',dir=r) as d:
  p=Path(artifact.get_entry('checkpoint.tar.gz').download(root=d));assert sha(p)==ref['sha256']
 summary['search_checkpoint_verified']=ref
plan=read(r/'protocol.json');bundle=r/'application/runs'/plan['experiment_id']
for arm in plan['arms']:
 d=bundle/'search'/arm['execution_id'];item={'execution_id':arm['execution_id'],'parent_best_deletions':arm['parent_best_deletions']}
 for name in ['progress.json','tracking.json']:
  if (d/name).exists():item[name.removesuffix('.json')]=read(d/name)
 if (d/'result.json').exists():
  result=read(d/'result.json');item['result']={k:result[k] for k in ['status','stop_reason','provider_spend_usd','continuation','stage_successful_literature_searches']}
  item['finalist']={k:v for k,v in result['finalist'].items() if k not in ['deleted_gene_ids']}
 summary['arms'][arm['arm']]=item
v=r/'validation'
if (v/'batch-progress.json').exists():
 summary['validation']=read(v/'batch-progress.json');summary['validation_launcher']=process(v/'launcher.json');summary['validation_lock']=lock_status(v/'batch.lock');summary['validation_jobs']={}
 for folder in (v/'runs').glob('*'):
  if not (folder/'experiments.json').exists():continue
  entry=read(folder/'experiments.json')['experiments'][0];item={'catalog':entry,'process':process(folder/'process.json')}
  backup=entry.get('data_backup',{})
  if backup.get('s3_checkpoint'):
   index=json.loads(remote(backup['s3_checkpoint']));progress=remote(index['files']['progress.json']);assert hashlib.sha256(progress).hexdigest()==backup['progress_sha256']
   artifact=api.artifact(backup['wandb_artifact']);assert artifact.state=='COMMITTED'
   with tempfile.TemporaryDirectory(prefix='monitor-',dir=r) as d:
    downloaded=Path(artifact.get_entry('progress.json').download(root=d));assert sha(downloaded)==backup['progress_sha256']
   data=json.loads(progress);item['verified_progress']=[{k:g[k] for k in ['name','generations_completed','terminal_reason']} for g in data['finalists']]
   item['artifact_verified']=True;item['s3_index']=backup['s3_checkpoint']
  summary['validation_jobs'][folder.name]=item
summary['free_disk_gib']=shutil.disk_usage(r).free/1024**3
rows=subprocess.check_output(['ps','-eo','pid,ppid,stat,etime,args'],text=True).splitlines()
summary['cell_processes']=[x for x in rows if 'ecoli_master_sim.py' in x and 'python' in x]
summary['budget_timer']=subprocess.check_output(['systemctl','list-timers','yggdrisil-budget-deadline.timer','--no-pager'],text=True)
encoded=(json.dumps(summary,indent=2)+'\n').encode()
if any(s in encoded for s in secrets):raise RuntimeError('Secret detected in compact monitoring metadata')
(r/'monitoring-summary.json').write_bytes(encoded)
print(json.dumps({'status':summary['search']['status'],'validation_status':summary.get('validation',{}).get('status'),'checked_at':summary['checked_at'],'checkpoint':ref['wandb_artifact'] if ref else None}))
