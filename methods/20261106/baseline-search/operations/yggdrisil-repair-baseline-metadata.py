import fcntl,hashlib,json,subprocess,tomllib
from pathlib import Path
from datetime import datetime,timezone
from importlib.metadata import distributions
r=Path('/home/ubuntu/yggdrisil-baselines-20261106');b=r/'bundle';sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
lock=(r/'batch.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert json.loads((r/'batch-progress.json').read_bytes())['status']=='completed_with_exceptions'
assert not list((b/'search').rglob('graph.sqlite')) and not list((b/'search').rglob('process.json')) and not list((b/'search').rglob('environment.json'))
rows=subprocess.check_output(['ps','-eo','pid,comm,args'],text=True).splitlines();assert not [x for x in rows if 'scripts.run_deletion_baseline' in x or 'yggdrisil-baseline-supervisor.py' in x]
files=json.loads((b/'source-manifest.json').read_bytes())['files']
for name,digest in files.items():assert sha(b/'source'/name)==digest
missing=[]
for d in distributions():
 if not d.metadata.get('Name'):
  missing.append(str(d._path))
assert len(missing)==2,missing
repairs=[]
for label,package in [('application','yggdrisil_ecoli'),('framework','yggdrisil')]:
 project=b/'source'/label;metadata=project/'src'/(package+'.egg-info')/'PKG-INFO';assert str(metadata.parent) in missing and not metadata.exists()
 spec=tomllib.loads((project/'pyproject.toml').read_text())['project'];assert spec['name'].replace('-','_')==package
 data=f"Metadata-Version: 2.1\nName: {spec['name']}\nVersion: {spec['version']}\nSummary: {spec['description']}\n\n";metadata.write_text(data)
 repairs.append({'path':str(metadata.relative_to(r)),'sha256':sha(metadata),'source_pyproject':str((project/'pyproject.toml').relative_to(r)),'pyproject_sha256':sha(project/'pyproject.toml'),'name':spec['name'],'version':spec['version']})
assert all(d.metadata.get('Name') and d.version for d in distributions())
assert all(sha(b/'source'/name)==digest for name,digest in files.items())
attempt=r/'attempts/package-metadata-before-search';attempt.mkdir(parents=True,exist_ok=False)
for name in ['launcher.json','batch-progress.json','supervisor.log','preflight.json','final-backup.json','monitoring-summary.json','monitoring.log']:
 p=r/name
 if p.exists():p.rename(attempt/name)
jobs={}
for job in json.loads((b/'jobs.json').read_bytes()):
 folder=b/job['directory'];jobs[job['run_id']]={'config_sha256':sha(folder/'config.json'),'launch_sha256':sha(folder/'launch.json'),'original_deadline':json.loads((folder/'launch.json').read_bytes())['deadline_unix']}
 log=folder/'controller.log';target=attempt/job['directory'];target.mkdir(parents=True);log.rename(target/'controller.log')
record={'status':'repaired','repaired_at':datetime.now(timezone.utc).isoformat(),'reason':'Frozen source snapshot contained .egg-info text files but omitted the PKG-INFO distribution header. Runtime inventory rejected nameless packages before any graph or scientific evaluation.','repair':'Added distribution Name/Version headers from exact frozen pyproject.toml; no scientific or framework Python source changed.','metadata_headers':repairs,'zero_scientific_states':True,'original84_source_hashes_unchanged':True,'jobs':jobs,'original_approval_and_deadlines_unchanged':True,'attempt_path':str(attempt)}
(r/'metadata-repair.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps({k:v for k,v in record.items() if k!='jobs'}))
