"""Read-only baseline progress, source and immutable checkpoint verification."""
import fcntl,hashlib,json,os,shutil,subprocess,tempfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
import boto3,wandb
r=Path('/home/ubuntu/yggdrisil-baselines-20261106');b=r/'bundle';read=lambda p:json.loads(p.read_bytes())
os.environ.update(read(r/'credentials/tracking.json'));s3=boto3.client('s3',region_name='us-east-1');api=wandb.Api(timeout=120)
sha=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
def process(record):
 d=dict(record);stat=Path(f"/proc/{d['pid']}/stat");d['alive']=stat.exists() and stat.read_text().split()[2]!='Z';d['identity_matches']=d['alive'] and stat.read_text().split()[21]==d['start_ticks'];return d

def remote(ref,path):
 u=urlparse(ref['uri']);s3.download_file(u.netloc,u.path.lstrip('/'),str(path));assert sha(path)==ref['sha256'] and path.stat().st_size==ref['bytes'],ref['uri']

source=read(b/'source-manifest.json')['files']
for name,digest in source.items():assert sha(b/'source'/name)==digest,name
for name,digest in read(r/'preflight.json')['operational_sources'].items():assert sha(r/name)==digest,name
summary={'experiment_id':'deletion-baselines-20261106','checked_at':datetime.now(timezone.utc).isoformat(),'worker':read(r/'worker.json'),'source_verified':True,'batch':read(r/'batch-progress.json'),'launcher':process(read(r/'launcher.json')),'jobs':[]}
with (r/'batch.lock').open() as f:
 try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:summary['batch_lock']='held'
 else:fcntl.flock(f,fcntl.LOCK_UN);summary['batch_lock']='free'
for job in read(b/'jobs.json'):
 folder=b/job['directory'];item={**job,'status':'not_started'}
 for name in ['progress.json','tracking.json','failure.json','completion.json']:
  if (folder/name).exists():item[name[:-5]]=read(folder/name)
 if (folder/'process.json').exists():item['process']=process(read(folder/'process.json'))
 if (folder/'result.json').exists():
  result=read(folder/'result.json');item['result']={k:result[k] for k in ['status','stop_reason','new_evaluated_states','provider_spend_usd']};item['finalist']=result['finalist']
 if (folder/'checkpoint.json').exists():
  ref=read(folder/'checkpoint.json');assert ref['status']=='verified'
  with tempfile.TemporaryDirectory(prefix='monitor-',dir=r) as temp:
   directory=Path(temp);remote(ref['archive'],directory/'checkpoint.tar.gz');remote(ref['index'],directory/'index.json');index=read(directory/'index.json');assert index['archive']==ref['archive'] and index['files']==ref['files']
   artifact=api.artifact(ref['wandb_artifact']);assert artifact.state=='COMMITTED'
   for name,expected in [('checkpoint.tar.gz',ref['archive']),('index.json',ref['index'])]:
    downloaded=Path(artifact.get_entry(name).download(root=str(directory/'wandb')));assert sha(downloaded)==expected['sha256']
  item['checkpoint']=ref;item['checkpoint_independently_verified']=True
 item['status']=item.get('completion',item.get('progress',{})).get('status','not_started');summary['jobs'].append(item)
summary['free_disk_gib']=shutil.disk_usage(r).free/1024**3;summary['deadline_timer']=subprocess.check_output(['systemctl','list-timers','yggdrisil-baseline-budget-stop.timer','--no-pager'],text=True)
if (r/'final-backup.json').exists():summary['final_backup']=read(r/'final-backup.json')
encoded=json.dumps(summary,indent=2)+'\n';secrets=[v for k,v in read(r/'credentials/tracking.json').items() if k.endswith('_KEY')];assert not any(s in encoded for s in secrets);(r/'monitoring-summary.json').write_text(encoded)
print(json.dumps({'status':summary['batch']['status'],'started':len(summary['batch']['jobs']),'verified_checkpoints':sum(x.get('checkpoint_independently_verified',False) for x in summary['jobs']),'checked_at':summary['checked_at']}))
