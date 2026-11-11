"""Local archival I/O only: audit frozen outputs without running scientific code."""
import fcntl, hashlib, json, netrc, os, shutil, sys, tarfile, tempfile, time
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
import boto3
repo=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');op=repo/'runs/vecoli-depth-matched-20261111'
sys.path[:0]=[str(repo),str(repo/'src')]
from scripts.supervise_baseline_depth_parallel import verified_download
from scripts.parallel_replication_validation import inspect_members
from yggdrisil_ecoli.data.io import file_sha256
lock=Path('/private/tmp/yggdrisil-matched-final-audit.lock').open('a+')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
os.environ['WANDB_BASE_URL']='https://api.forge.coreweave.com'
login=netrc.netrc().authenticators('api.wandb.ai')
if not login: raise RuntimeError('Authorized W&B tracking login unavailable')
os.environ['WANDB_API_KEY']=login[2]
os.environ.pop('WANDB_SERVICE',None)
import wandb
s3=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts').get_caller_identity()['Account'];bucket='phd-research-storage-1758274488';prefix='yggdrisil/experiments/'+op.name+'/'
store=SimpleNamespace(s3=s3,owner=owner,bucket=bucket)
latest=json.loads(sorted(op.glob('worker-state-20*.json'))[-1].read_bytes())
if any(x['state']!='stopped' for x in latest['instances']): raise RuntimeError('Expected completed stopped workers')
role='primary';objects=latest['final_objects'][role];indexes=[x for x in objects if x['key'].endswith('.json')]
if len(indexes)!=1: raise RuntimeError('Ambiguous final index')
x=indexes[0];ref={'uri':'s3://'+bucket+'/'+x['key'],'sha256':Path(x['key']).stem,'bytes':x['bytes']}
with tempfile.TemporaryDirectory(prefix='matched-final-archive-',dir='/private/tmp') as temp:
 root=Path(temp)
 verified_download(store,ref,root/'index.json');index=json.loads((root/'index.json').read_bytes());archive=index['archive'];u=urlparse(archive['uri'])
 if not u.path.startswith('/'+prefix+role+'/final/') or u.netloc!=bucket: raise RuntimeError('Unexpected archive location')
 if shutil.disk_usage(root).free < archive['bytes']*2+2*1024**3: raise RuntimeError('Insufficient local archival workspace')
 receipt={'archive':archive,'index':ref,'wandb_artifact':'mcclain/yggdrisil/primary-final:v0'}
 (op/'primary-final-index.json').write_bytes((root/'index.json').read_bytes())
 print('Downloading primary final S3 archive for full verification',flush=True)
 verified_download(store,archive,root/'data.tar.gz')
 inspect_members(root/'data.tar.gz',index)
 print('Verified S3 archive and every indexed member',flush=True)
 metadata={}
 names={'progress.json','results.json','s3-checkpoint.json','experiments.json','manifest.json','process.json','handoff-progress.json'}
 with tarfile.open(root/'data.tar.gz') as packed:
  for member in packed:
   path=Path(member.name)
   wanted=(len(path.parts)==3 and path.parts[0]=='runs' and path.name in names) or member.name=='operation/handoff-progress.json'
   if not wanted: continue
   if member.size>8*1024**2: raise RuntimeError('Unexpected oversized compact metadata')
   data=packed.extractfile(member).read();value=json.loads(data);metadata[member.name]={'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'value':value}
 (op/'primary-final-compact-metadata.json').write_text(json.dumps({'archive':archive,'index':ref,'files':metadata},indent=2)+'\n')
 with tempfile.TemporaryDirectory(prefix='matched-final-wandb-',dir='/private/tmp') as cache:
  os.environ['WANDB_CACHE_DIR']=cache
  remote=wandb.Api(timeout=120).artifact(receipt['wandb_artifact'])
  if remote.state!='COMMITTED': raise RuntimeError('Final W&B artifact is not committed')
  print('Downloading committed W&B final artifact without cache',flush=True)
  for name,key in [('data.tar.gz','archive'),('index.json','index')]:
   target=Path(remote.get_entry(name).download(root=str(root/'wandb'),skip_cache=True))
   if file_sha256(target)!=receipt[key]['sha256'] or target.stat().st_size!=receipt[key]['bytes']: raise RuntimeError('W&B final bytes mismatch')
  proof={'status':'verified','at_unix':time.time(),'receipt':receipt,'members':len(index['files']),'member_bytes':sum(x['bytes'] for x in index['files']),'full_s3_get_verified':True,'every_indexed_member_verified':True,'wandb_uncached_download_verified':True,'wandb_state':remote.state,'wandb_artifact_id':remote.id,'wandb_digest':remote.digest,'wandb_version':remote.version,'audited_worker_instance':'i-05b12ad155a4aff74','verification_host':'Mac archival I/O only','no_scientific_execution':True,'method_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'member_auditor_sha256':file_sha256(repo/'scripts/parallel_replication_validation.py')}
 (op/'primary-final-independent-verification.json').write_text(json.dumps(proof,indent=2)+'\n')
 (op/'source'/Path(__file__).name).write_bytes(Path(__file__).read_bytes())
 print(json.dumps(proof),flush=True)
