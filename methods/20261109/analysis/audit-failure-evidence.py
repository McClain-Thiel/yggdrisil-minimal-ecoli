import gzip,hashlib,importlib.util,json,math,os,tempfile,time
from pathlib import Path
from urllib.parse import urlparse
from collections import Counter
import pyarrow.parquet as pq
R=Path('/home/ubuntu/yggdrisil-replication-20261109');V=R/'validation';read=lambda p:json.loads(p.read_bytes());sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
spec=importlib.util.spec_from_file_location('parallel',V/'supervisor.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.activate_paths();w=read(V/'parallel-worker.json');seed=w['seed'];os.environ.update(read(V/'credentials/tracking.json'));s3,owner=m.client()
import wandb
for n,h in read(V/'parallel-runtime-proof.json')['files'].items():assert sha(R/n)==h,n
for n,h in read(V/'deployment-manifest.json')['files'].items():assert sha(V/n)==h,n
folder=V/'runs'/f'seed-{seed}';catalog=read(folder/'experiments.json')['experiments'][0];backup=catalog['data_backup'];ref=backup['s3_checkpoint'];u=urlparse(ref['uri']);body=s3.get_object(Bucket=u.netloc,Key=u.path.lstrip('/'),ExpectedBucketOwner=owner)['Body']
try:raw=body.read()
finally:body.close()
assert len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256'];index=json.loads(raw);artifact=wandb.Api(timeout=120).artifact(backup['wandb_artifact']);assert artifact.state=='COMMITTED'
report={'checked_at_unix':time.time(),'seed':seed,'instance_id':w['instance_id'],'runtime_hashes_verified':True,'checkpoint':ref,'wandb_artifact':backup['wandb_artifact'],'committed':True,'checkpoint_index_full_get_verified':True,'scope':'Independent full downloads of pinned progress and every stopped-lineage stderr and available terminal-cell mass-history file from both S3 and uncached W&B. Read-only analysis; no simulation or scientific repair.','files':{},'lineages':[]}
with tempfile.TemporaryDirectory(dir=V) as temp:
 def verify(name):
  if name in report['files']:return Path(temp)/'downloaded'/name
  entry=index['files'][name];dest=Path(temp)/'downloaded'/name;dest.parent.mkdir(parents=True,exist_ok=True);m.download(s3,owner,entry,dest)
  wb=Path(artifact.get_entry(name).download(root=temp+'/wandb',skip_cache=True));assert sha(wb)==entry['sha256'] and wb.stat().st_size==entry['bytes'],name
  report['files'][name]=entry;return dest
 progress=read(verify('progress.json'));assert report['files']['progress.json']['sha256']==backup['progress_sha256']
 columns=['global_time','listeners__mass__cell_mass','listeners__mass__dry_mass','listeners__mass__dry_mass_fold_change']
 for x in progress['finalists']:
  reason=x['terminal_reason']
  if reason not in ['model_exception','nondivision_max_duration','resource_failure','orchestration_failure']:continue
  task=x['terminal_task'];variant=x['variant_index'];generation=task['generation'];wd=Path(task['workdir']);suffix='/'+'/'.join(wd.parts[-2:])+'/.command.err';names=[n for n in index['files'] if n.endswith(suffix)];assert len(names)==1,(seed,variant,names)
  stderr_path=verify(names[0]);assert sha(stderr_path)==task['stderr_sha256'];assert (wd/'.exitcode').is_file();assert int((wd/'.exitcode').read_text().strip())==task['exit_code'];assert sha(wd/'.command.err')==sha(stderr_path)
  stderr=stderr_path.read_text(errors='replace');last=stderr.strip().splitlines()[-1] if stderr.strip() else ''
  marker=f'/variant={variant}/lineage_seed={seed}/generation={generation}/';hist_names=[n for n in index['files'] if '/history/' in n and marker in n and n.endswith('.pq')]
  records=[]
  initial_rows_without_global_time=0
  for name in hist_names:
   path=verify(name);table=pq.ParquetFile(path);missing=set(columns)-set(table.schema_arrow.names)
   if missing and not (missing=={'global_time'} and table.metadata.num_rows==1 and path.stem=='1'):
    raise ValueError(f'Unexpected missing terminal history fields: {name}: {sorted(missing)}')
   chunk=table.read(columns=columns).to_pylist()
   if missing:
    chunk[0]['global_time']=None;initial_rows_without_global_time+=1
   records.extend(chunk)
  finite_times=[r['global_time'] for r in records if r['global_time'] is not None and math.isfinite(r['global_time'])]
  nonfinite={col:sum(r[col] is None or not math.isfinite(r[col]) for r in records) for col in columns}
  nonfinite['global_time']=sum(r['global_time'] is not None and not math.isfinite(r['global_time']) for r in records)
  timed=sorted([r for r in records if r['global_time'] is not None and math.isfinite(r['global_time'])],key=lambda r:r['global_time'])
  initial_time=x['generations'][-1]['division_global_time_s'] if x.get('generations') else 0.0
  finite_output={k:(v if v is None or math.isfinite(v) else None) for k,v in (timed[-1] if timed else {}).items()}
  category='observed_nondivision' if reason=='nondivision_max_duration' else 'unresolved_model_or_numerical_failure'
  if "AttributeError: 'list' object has no attribute 'T'" in last:category='software_error_handling_path'
  if reason in ['resource_failure','orchestration_failure']:category='infrastructure_or_orchestration_failure'
  warnings=Counter(line.strip()[:250] for line in stderr.splitlines() if any(t in line for t in ['RuntimeWarning:','UserWarning:','ODEintWarning:']))
  report['lineages'].append({'name':x['name'],'state_id':x['state_id'],'deletion_count':x['deletion_count'],'variant_index':variant,'simulation_seed':seed,'completed_divisions':x['generations_completed'],'failed_generation':generation,'raw_terminal_reason':reason,'audit_category':category,'causal_genotype_attribution':'not_established_by_exception_or_time_cap_alone','exception_last_line':last,'stderr_sha256':sha(stderr_path),'stderr_s3_reference':index['files'][names[0]],'history_file_count':len(hist_names),'history_rows':len(records),'initial_rows_without_global_time':initial_rows_without_global_time,'nonfinite_mass_or_time_values':nonfinite,'mass_history_finite':bool(records) and not any(nonfinite.values()),'last_emitted_elapsed_simulated_seconds':max(finite_times)-initial_time if finite_times else None,'last_emitted_values':finite_output,'last_closed_cycle_metrics':x.get('late_cycle_metrics'),'warnings':dict(warnings),'terminal_history_files':hist_names,'numerical_diagnostics_present':[n for n in index['files'] if '/'+ '/'.join(wd.parts[-2:])+'/solver-diagnostics/' in n]})
 report.update(verified_at_unix=time.time(),status='passed',object_count=len(report['files']),bytes_per_store=sum(x['bytes'] for x in report['files'].values()))
 m.save(R/'failure-evidence-audit.json',report)
 print(json.dumps(report,allow_nan=False),flush=True)
