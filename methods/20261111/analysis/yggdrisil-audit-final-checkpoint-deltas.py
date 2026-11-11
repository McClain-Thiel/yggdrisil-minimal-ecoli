"""Verify every final checkpoint entry, reusing only proven identical immutable content."""
import concurrent.futures,fcntl,hashlib,json,netrc,os,tempfile,time
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
import boto3
op=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/vecoli-depth-matched-20261111');bucket='phd-research-storage-1758274488';prefix='/yggdrisil/experiments/'+op.name+'/'
lock=Path('/private/tmp/yggdrisil-matched-final-checkpoint.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
os.environ['WANDB_BASE_URL']='https://api.forge.coreweave.com';auth=netrc.netrc().authenticators('api.wandb.ai')
if not auth:raise RuntimeError('Tracking login unavailable')
os.environ['WANDB_API_KEY']=auth[2];os.environ.pop('WANDB_SERVICE',None)
import wandb
s3=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts').get_caller_identity()['Account'];api=wandb.Api(timeout=120)
def get(ref):
 u=urlparse(ref['uri'])
 if u.netloc!=bucket or not u.path.startswith(prefix):raise ValueError('Unexpected checkpoint location')
 body=s3.get_object(Bucket=bucket,Key=u.path.lstrip('/'),ExpectedBucketOwner=owner)['Body']
 try:raw=body.read()
 finally:body.close()
 if len(raw)!=ref['bytes'] or hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise RuntimeError('S3 checksum mismatch')
 return raw
current=json.loads((op/'final-checkpoint-metadata.json').read_bytes())
report={'at':datetime.now(timezone.utc).isoformat(),'method':'All entries covered: identical immutable S3 objects reuse prior full-GET proofs; unchanged W&B entries additionally require identical SHA256/size in S3 indexes and identical digest/size in both committed W&B manifests. Every new or changed entry is freshly downloaded from both S3 and W&B and SHA256 checked. Full final archive/member downloads are verified separately.','no_scientific_execution':True,'workflows':{}}
for name,workflow in current['workflows'].items():
 role='secondary' if name=='seed-307' else 'primary';prior_path=op/(role+'-independent-checkpoint-verification.json');prior=json.loads(prior_path.read_bytes())['workflows'][name]
 assert prior['verification_status']=='verified' and prior['all_indexed_files_verified'] and prior['wandb_uncached_download_verified']
 index=json.loads(get(workflow['checkpoint']));old_index=json.loads(get(prior['checkpoint']))
 latest=api.artifact('mcclain/yggdrisil/'+op.name+'-'+name+'-data:latest');previous=api.artifact(prior['wandb_artifact'])
 if latest.state!='COMMITTED' or previous.state!='COMMITTED':raise RuntimeError('Uncommitted artifact')
 pinned='mcclain/yggdrisil/'+op.name+'-'+name+'-data:'+latest.version
 old_entries=previous.manifest.entries;new_entries=latest.manifest.entries
 reuse=[];changed=[]
 for member,ref in index['files'].items():
  path=Path(member)
  if path.is_absolute() or '..' in path.parts or member not in new_entries:raise RuntimeError('Unsafe or missing artifact member')
  old_ref=old_index['files'].get(member);old_entry=old_entries.get(member);new_entry=new_entries[member]
  if old_ref==ref and old_entry and old_entry.digest==new_entry.digest and old_entry.size==new_entry.size==ref['bytes']:
   reuse.append({'path':member,'sha256':ref['sha256'],'bytes':ref['bytes'],'wandb_entry_digest':new_entry.digest})
  else:changed.append((member,ref))
 with tempfile.TemporaryDirectory(prefix='matched-checkpoint-delta-',dir='/private/tmp') as tmp:
  def verify(item):
   member,ref=item;get(ref);target=Path(latest.get_entry(member).download(root=tmp,skip_cache=True))
   if target.is_symlink() or not target.resolve().is_relative_to(Path(tmp).resolve()):raise RuntimeError('Unsafe downloaded artifact path')
   with target.open('rb') as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
   if sha!=ref['sha256'] or target.stat().st_size!=ref['bytes']:raise RuntimeError('W&B checksum mismatch: '+member)
   return {'path':member,'sha256':sha,'bytes':ref['bytes']}
  with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:fresh=list(pool.map(verify,changed))
 # A current COMMITTED manifest is immutable; explicitly pin its version and identity in the proof.
 proof={'status':'verified_all_entries','checkpoint':workflow['checkpoint'],'wandb_artifact':pinned,'wandb_state':latest.state,'wandb_id':latest.id,'wandb_digest':latest.digest,'files':len(index['files']),'bytes':sum(x['bytes'] for x in index['files'].values()),'fresh_s3_and_uncached_wandb_downloads':fresh,'reused_immutable_entries':reuse,'prior_full_download_proof_sha256':hashlib.sha256(prior_path.read_bytes()).hexdigest(),'prior_checkpoint':prior['checkpoint'],'prior_wandb_artifact':prior['wandb_artifact'],'verified_at':datetime.now(timezone.utc).isoformat()}
 assert len(reuse)+len(fresh)==len(index['files'])
 report['workflows'][name]=proof
 (op/'final-checkpoint-independent-verification.json').write_text(json.dumps(report,indent=2)+'\n')
 print(name,pinned,'fresh',len(fresh),'immutable_reused',len(reuse),flush=True)
report['status']='verified_all_final_checkpoint_entries';report['method_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();(op/'final-checkpoint-independent-verification.json').write_text(json.dumps(report,indent=2)+'\n');(op/'source'/Path(__file__).name).write_bytes(Path(__file__).read_bytes());print(report['status'],flush=True)
