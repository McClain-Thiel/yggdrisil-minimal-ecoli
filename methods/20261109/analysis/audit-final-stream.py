"""Stream final private archives without retaining scientific bulk data locally."""
import gzip,hashlib,io,json,math,os,re,subprocess,sys,tarfile,time
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
import requests
import pyarrow.parquet as pq
R=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');O=R/'runs/search-replication-20261109';D=O/'parallel-validation/finalization';seed=sys.argv[1]
def read(p):return json.loads(p.read_bytes())
def save(p,v):
 p.parent.mkdir(parents=True,exist_ok=True);t=p.with_suffix('.pending');t.write_text(json.dumps(v,indent=2,sort_keys=True,allow_nan=False)+'\n');os.replace(t,p)
def aws(*args):
 p=subprocess.run(['aws',*args],capture_output=True,text=True)
 if p.returncode:raise RuntimeError('AWS command failed: '+args[0]+' '+args[1]+': '+p.stderr[:300])
 return p.stdout.strip()
owner=json.loads(aws('sts','get-caller-identity','--output','json'))['Account'];aws('s3api','head-bucket','--bucket','phd-research-storage-1758274488','--expected-bucket-owner',owner)
if not seed.isdigit():w={'final_backup':read(D/f'receipt-{seed}.json')}
elif seed=='101':w=read(D/'coordinator-status.json')['workers']['101']
else:w=read(D/f'workers-{seed}-status.json')
receipt=w['final_backup'];assert receipt['status']=='verified';save(D/f'receipt-{seed}.json',receipt)
session=requests.Session();session.trust_env=False
class Reader:
 def __init__(self,raw):self.raw=raw;self.h=hashlib.sha256();self.n=0
 def read(self,n=-1):
  b=self.raw.read(n);self.h.update(b);self.n+=len(b);return b
 def verify(self,ref):
  while self.read(1024*1024):pass
  if self.n!=ref['bytes'] or self.h.hexdigest()!=ref['sha256']:raise RuntimeError('Full stream byte count/SHA256 mismatch')
class RangeStream:
 def __init__(self,url,size):self.url=url;self.size=size;self.pos=0;self.buf=b'';self.etag=None
 def read(self,n=-1):
  if n<0:n=self.size-self.pos
  if not n or self.pos>=self.size:return b''
  if not self.buf:
   end=min(self.pos+8*1024*1024,self.size)-1
   for attempt in range(3):
    try:
     headers={'Range':f'bytes={self.pos}-{end}'}
     if self.etag:headers['If-Match']=self.etag
     with session.get(self.url,headers=headers,timeout=(30,60)) as resp:
      if resp.status_code!=206:raise RuntimeError('Expected partial-content response, got '+str(resp.status_code))
      if resp.headers.get('Content-Range')!=f'bytes {self.pos}-{end}/{self.size}':raise RuntimeError('Range bounds mismatch')
      etag=resp.headers.get('ETag')
      if self.etag is not None and etag!=self.etag:raise RuntimeError('Object changed during streaming')
      self.etag=etag;block=resp.content
      if len(block)!=end-self.pos+1:raise RuntimeError('Short range read')
      self.buf=block
     break
    except (requests.RequestException,RuntimeError) as exc:
     print(json.dumps({'seed':seed,'range_retry':attempt+1,'offset':self.pos,'error_type':type(exc).__name__}),flush=True)
     if attempt==2:raise RuntimeError('Artifact range read failed after three attempts') from None
   if self.pos%(128*1024*1024)==0:print(json.dumps({'seed':seed,'stream_bytes':self.pos,'total':self.size}),flush=True)
  out=self.buf[:n];self.buf=self.buf[len(out):];self.pos+=len(out);return out
 def __enter__(self):return self
 def __exit__(self,*args):return False
def response(url):
 try:x=session.get(url,stream=True,timeout=(30,120));x.raise_for_status()
 except requests.RequestException as e:raise RuntimeError('Private artifact transfer failed: '+type(e).__name__) from None
 return x
def s3_url(ref):return aws('s3','presign',ref['uri'],'--expires-in','3600')
def get_small(ref):
 with response(s3_url(ref)) as resp:
  raw=resp.content
 if len(raw)!=ref['bytes'] or hashlib.sha256(raw).hexdigest()!=ref['sha256']:raise RuntimeError('Index checksum mismatch')
 return raw
idxraw=get_small(receipt['index']);idx=json.loads(idxraw);assert idx['archive']==receipt['archive'];save(D/f'index-{seed}.json',idx)
expected={x['path']:x for x in idx['files']};assert len(expected)==len(idx['files'])==receipt['members'];seen=set();history={};warnings=Counter();metadata={}
report={'seed':int(seed) if seed.isdigit() else seed,'started_at':datetime.now(timezone.utc).isoformat(),'receipt':receipt,'independent_verifier':'audit-final-stream.py','source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'method':'Fresh full-object HTTPS GET via verified contiguous ranges; no SDK cache; all S3 tar members checked; identical W&B archive SHA256 binds the same members. Bulk scientific data not retained on Mac.','status':'s3_audit_in_progress'}
save(D/f'independent-final-audit-{seed}.json',report);print(json.dumps({'seed':seed,'stage':'s3_archive_stream_started','bytes':receipt['archive']['bytes']}),flush=True)
with RangeStream(s3_url(receipt['archive']),receipt['archive']['bytes']) as resp:
 stream=Reader(resp)
 with tarfile.open(fileobj=stream,mode='r|gz') as tf:
  for member in tf:
   name=member.name;p=Path(name)
   if not member.isfile() or p.is_absolute() or '..' in p.parts or name not in expected or name in seen:raise RuntimeError('Unsafe, duplicate or unexpected archive member: '+name)
   if any(x in p.parts for x in ('credentials','.aws','.ssh','source')) or p.suffix in ('.py','.sh','.pem') or p.name in ('.env','.netrc'):raise RuntimeError('Excluded source/credential path in data archive')
   f=tf.extractfile(member);assert f is not None;h=hashlib.sha256();size=0
   is_hist=seed.isdigit() and '/history/' in name and name.endswith('.pq')
   is_meta=name in [f'runs/seed-{seed}/progress.json',f'runs/seed-{seed}/manifest.json','knockout-audit/variant-knockout-audit.json']
   capture=is_hist or is_meta or p.name=='.command.err';buf=io.BytesIO() if capture else None
   while block:=f.read(1024*1024):
    h.update(block);size+=len(block)
    if buf is not None:buf.write(block)
   ref=expected[name]
   if size!=ref['bytes'] or h.hexdigest()!=ref['sha256']:raise RuntimeError('Member checksum mismatch: '+name)
   seen.add(name)
   if is_meta:
    value=json.loads(buf.getvalue());metadata[name]={'sha256':h.hexdigest(),'bytes':size}
    if p.name=='manifest.json':
     value={k:v for k,v in value.items() if k not in ['finalists']};value['full_manifest_reference']=ref
    save(D/f'metadata-{seed}'/p.name,value)
   if is_hist:
    mat=re.search(r'/variant=(\d+)/lineage_seed=(\d+)/generation=(\d+)/',name)
    if not mat:raise RuntimeError('Unrecognized history identity: '+name)
    v,s,g=map(int,mat.groups());key=f'{v}/{s}/{g}';stats=history.setdefault(key,{'variant':v,'seed':s,'generation':g,'files':0,'rows':0,'nonfinite_mass_values':0,'nonfinite_time_values':0,'initial_rows_without_time':0,'first_time':None,'last_time':None,'first_dry_mass':None,'last_dry_mass':None,'first_cell_mass':None,'last_cell_mass':None})
    table=pq.ParquetFile(io.BytesIO(buf.getvalue()));cols=['listeners__mass__cell_mass','listeners__mass__dry_mass','listeners__mass__dry_mass_fold_change'];missing=set(cols)-set(table.schema_arrow.names)
    if missing:raise RuntimeError('Missing mass columns: '+name)
    has_time='global_time' in table.schema_arrow.names
    if not has_time and not(table.metadata.num_rows==1 and p.stem=='1'):raise RuntimeError('Unexpected missing history clock: '+name)
    data=table.read(columns=cols+(['global_time'] if has_time else [])).to_pydict();n=table.metadata.num_rows;stats['files']+=1;stats['rows']+=n
    stats['nonfinite_mass_values']+=sum(x is None or not math.isfinite(x) for c in cols for x in data[c]);stats['initial_rows_without_time']+=0 if has_time else n
    for j,t in enumerate(data.get('global_time',[])):
     if t is None or not math.isfinite(t):stats['nonfinite_time_values']+=1;continue
     for bound,cmp in [('first',lambda a,b:a<b),('last',lambda a,b:a>b)]:
      if stats[bound+'_time'] is None or cmp(t,stats[bound+'_time']):
       stats[bound+'_time']=t;stats[bound+'_dry_mass']=data[cols[1]][j];stats[bound+'_cell_mass']=data[cols[0]][j]
   if p.name=='.command.err':
    for line in buf.getvalue().decode(errors='replace').splitlines():
     if any(s in line for s in ['RuntimeWarning:','UserWarning:','ODEintWarning:']):warnings[line.strip()[:300]]+=1
 stream.verify(receipt['archive'])
assert seen==set(expected)
report.update(s3_archive_full_get_verified=True,index_full_get_verified=True,indexed_members_verified=len(seen),indexed_member_bytes=sum(x['bytes'] for x in idx['files']),history_cells=list(history.values()),history_files=sum(x['files'] for x in history.values()),history_rows=sum(x['rows'] for x in history.values()),warnings=dict(warnings),metadata=metadata,status='s3_passed_wandb_pending');save(D/f'independent-final-audit-{seed}.json',report);print(json.dumps({'seed':seed,'stage':'s3_members_passed','members':len(seen),'history_rows':report['history_rows']}),flush=True)
os.environ.update(read(O/'credentials/tracking.json'))
import wandb
artifact=wandb.Api(timeout=120).artifact(receipt['wandb_artifact']);assert artifact.state=='COMMITTED';files={x.name:x for x in artifact.files()};assert set(files)=={'data.tar.gz','index.json'}
for name,ref in [('index.json',receipt['index']),('data.tar.gz',receipt['archive'])]:
 f=files[name];assert f.size==ref['bytes']
 with RangeStream(f.direct_url,ref['bytes']) as resp:
  stream=Reader(resp);stream.verify(ref)
 print(json.dumps({'seed':seed,'stage':'wandb_full_stream_passed','name':name,'bytes':ref['bytes']}),flush=True)
report.update(wandb_state='COMMITTED',wandb_uncached_full_get_verified=True,status='passed',verified_at=datetime.now(timezone.utc).isoformat());save(D/f'independent-final-audit-{seed}.json',report)
print(json.dumps({'seed':seed,'status':'passed','members':len(seen),'archive_sha256':receipt['archive']['sha256']}),flush=True)
