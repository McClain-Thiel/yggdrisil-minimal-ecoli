import concurrent.futures,hashlib,io,json,math,os,subprocess,time,sys
from pathlib import Path
from urllib.parse import urlparse
import requests,pyarrow.parquet as pq
R=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');O=R/'runs/search-replication-20261109';D=O/'parallel-validation/finalization';os.environ.update(json.loads((O/'credentials/tracking.json').read_bytes()))
import wandb
api=wandb.Api(timeout=120);catalog=json.loads((D/'catalog/validation/parallel-results.json').read_bytes());reports={};calibration='--calibration' in sys.argv
for seed,w in catalog['workers'].items():
 if calibration and seed!='101':continue
 label=('calibration-worker-' if calibration else 'seed-')+seed
 backup=w['workflows'][label]['experiments.json']['experiments'][0]['data_backup'];ref=backup['s3_checkpoint'];dest=D/f'checkpoint-index-{label}.json';subprocess.run(['aws','s3','cp',ref['uri'],str(dest),'--only-show-errors'],check=True);raw=dest.read_bytes();assert len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256'];idx=json.loads(raw);candidates=[n for n in idx['files'] if '/history/' in n and f'/lineage_seed={seed}/' in n and n.endswith('.pq')];first={}
 for n in candidates:
  key=str(Path(n).parent)
  if key not in first or int(Path(n).stem)<int(Path(first[key]).stem):first[key]=n
 names=list(first.values());assert names
 artifact=api.artifact(backup['wandb_artifact']);assert artifact.state=='COMMITTED';files={f.name:f for f in artifact.files(names=names)};assert set(files)==set(names)
 def verify(n):
  expected=idx['files'][n]
  for attempt in range(3):
   try:
    sess=requests.Session();sess.trust_env=False
    with sess.get(files[n].direct_url,timeout=(20,60)) as response:
     if response.status_code!=200:raise RuntimeError('Unexpected file response '+str(response.status_code))
     b=response.content
    if len(b)!=expected['bytes'] or hashlib.sha256(b).hexdigest()!=expected['sha256']:raise RuntimeError('Initial history hash mismatch')
    break
   except (requests.RequestException,RuntimeError) as e:
    print(json.dumps({'seed':seed,'retry':attempt+1,'error_type':type(e).__name__}),flush=True)
    if attempt==2:raise RuntimeError('Initial clock verification download failed') from None
  table=pq.ParquetFile(io.BytesIO(b));cols=table.schema_arrow.names
  times=table.read(columns=['global_time']).to_pydict()['global_time'] if 'global_time' in cols else [None]*table.metadata.num_rows
  if not times or times[0] is not None or any(t is None or not math.isfinite(t) for t in times[1:]):raise RuntimeError('Unexpected initial timestamp pattern: '+n)
  return {'path':n,'sha256':expected['sha256'],'initial_null_clock_rows':1,'remaining_rows_finite':True,'rows':len(times)}
 with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(verify,names))
 report={'seed':int(seed),'status':'passed','checkpoint':ref,'wandb_artifact':backup['wandb_artifact'],'committed':True,'scope':'Each full-run cell initial history block downloaded uncached from W&B and SHA256 matched to the pinned S3 checkpoint. Exactly one null pre-clock emission per cell; all remaining timestamps finite. Other blocks were checked during full archive audit.','initial_history_files':rows,'verified_at_unix':time.time()};(D/f'initial-clock-audit-{label if calibration else seed}.json').write_text(json.dumps(report,indent=2)+'\n');reports[seed]={'cells':len(rows),'rows':sum(x['rows'] for x in rows)};print(json.dumps({'seed':seed,'passed':reports[seed]}),flush=True)
print(json.dumps(reports),flush=True)
