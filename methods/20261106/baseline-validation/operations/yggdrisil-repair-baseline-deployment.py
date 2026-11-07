import hashlib,json,subprocess
from pathlib import Path
op=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/vecoli-deletion-baselines-20gen-3seed-20261106');root='/home/ubuntu/yggdrisil-baseline-validation-20261106'
ssh=['ssh','-i',str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),'-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ClearAllForwardings=yes','ubuntu@34.201.34.222']
source=op/'source/supervisor.py';text=source.read_text().replace("for label in ['runs', 'inputs', 'knockout-audit', 'attempts']:\n", "for label in ['runs', 'inputs', 'knockout-audit', 'attempts']:\n        if not (root / label).exists():\n            continue\n");source.write_text(text)
source=op/'source/preflight.py';text=source.read_text().replace("id='vecoli-baselines-preflight-20261106'","id='vecoli-baselines-preflight-20261106-retry1'");text=text.replace("'controller_and_solver_guards':12", "'controller_and_solver_guards':12,'launch_guards':2,'protein_pattern_scanner_guards':4");source.write_text(text)
prepare=r'''
import fcntl,hashlib,json,shutil
from pathlib import Path
from datetime import datetime,timezone
root=Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')
lock=(root/'batch.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
for p in Path('/proc').iterdir():
 if p.name.isdigit():
  try:cmd=(p/'cmdline').read_bytes().split(b'\0')
  except FileNotFoundError:continue
  if any(x.endswith((b'/supervisor.py',b'/track_vecoli_experiment.py',b'/ecoli_master_sim.py')) and str(root).encode() in x for x in cmd):raise RuntimeError('Existing scientific process: '+p.name)
old=root/'attempts/credential-pattern-calibration';old.mkdir(parents=True,exist_ok=False)
(old/'runs').mkdir();shutil.move(str(root/'runs/calibration'),str(old/'runs/calibration'))
for name in ['batch-progress.json','launcher.json','supervisor.log','monitoring-summary.json','preflight.json','prepared.json','deployment-manifest.json']:
 shutil.move(str(root/name),str(old/name))
for name in ['application/src/yggdrisil_ecoli/baseline_storage.py','supervisor.py','preflight.py']:
 p=old/'source'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/name,p)
folder=root/'runs/calibration';folder.mkdir()
manifest=json.loads((old/'runs/calibration/manifest.json').read_bytes());workflow=json.loads((old/'runs/calibration/workflow.json').read_bytes())
exp=manifest['experiment_id']+'-retry1';manifest['experiment_id']=exp
workflow['experiment_id']=exp.replace('-','_')
manifest['workflow']['experiment_id']=workflow['experiment_id']
workflow['emitter_arg']['out_dir']=str(folder/'output')
workflow_path=folder/'workflow.json';workflow_path.write_text(json.dumps(workflow,sort_keys=True,indent=2)+'\n')
manifest['workflow']['config_sha256']=hashlib.sha256(workflow_path.read_bytes()).hexdigest()
manifest['tracking'].update(run_id=hashlib.sha256(exp.encode()).hexdigest()[:12],run_name=exp,artifact_name=exp+'-data',s3_prefix=manifest['tracking']['s3_prefix']+'-retry1')
manifest['operational_retry']={'reason':'Credential heuristic falsely identified native protein sequences; no configured secret present; original attempt preserved','previous_manifest_sha256':hashlib.sha256((old/'runs/calibration/manifest.json').read_bytes()).hexdigest(),'science_unchanged':True}
(folder/'manifest.json').write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
(folder/'experiments.json').write_text(json.dumps({'experiments':[{'id':exp,'status':'prepared','data_backup':{}}]},indent=2)+'\n')
for name in ['runscripts','ecoli']:(folder/name).symlink_to(root/'native-validated'/name,target_is_directory=True)
prepared=json.loads((old/'prepared.json').read_bytes());prepared['manifest_sha256']['calibration']=hashlib.sha256((folder/'manifest.json').read_bytes()).hexdigest();(root/'prepared.json').write_text(json.dumps(prepared,indent=2)+'\n')
record={'repaired_at':datetime.now(timezone.utc).isoformat(),'attempt':str(old),'reason':'74 AWS-ID-shaped substrings in native protein data; zero configured credential matches. Full reference sequence hashes resolve only those exact collisions.','science_unchanged':True,'original_deadlines_unchanged':True,'same_instance':'i-0e673348d39a6b805','new_ip':'34.201.34.222','calibration_retry_experiment':exp,'backup_of_first_attempt_pending':True}
(root/'setup-repair.json').write_text(json.dumps(record,indent=2)+'\n')
print('Original attempt and source preserved; new control folder and W&B identity prepared.')
'''
subprocess.run(ssh+['python3 -'],input=prepare.encode(),check=True)
files={'application/src/yggdrisil_ecoli/baseline_storage.py':op/'deployment/validation-application/src/yggdrisil_ecoli/baseline_storage.py','supervisor.py':op/'source/supervisor.py','preflight.py':op/'source/preflight.py','test_native_pattern_screen.py':op/'source/test_native_pattern_screen.py','credential-pattern-policy.json':op/'credential-pattern-policy.json'}
for name,path in files.items():subprocess.run(ssh+['cat > '+root+'/'+name],input=path.read_bytes(),check=True)
original=(op/'deployment-manifest.json').read_bytes();(op/'attempts'/('deployment-manifest-'+hashlib.sha256(original).hexdigest()+'.json')).write_bytes(original)
manifest=json.loads(original)
for name,path in files.items():manifest['files'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
manifest['operational_repair']='Exact native protein context hashes resolve heuristic false positives; full configured-secret checks unchanged. First attempt retained separately.'
p=op/'deployment-manifest.json';p.write_text(json.dumps(manifest,indent=2)+'\n');subprocess.run(ssh+['cat > '+root+'/deployment-manifest.json'],input=p.read_bytes(),check=True)
cmd='cd '+root+' && export PYTHONPATH='+root+'/application/src:'+root+'/application:'+root+'/framework/src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYDANTIC_DISABLE_PLUGINS=__all__; env/bin/python -m pytest test_launch_guards.py test_native_pattern_screen.py tests/test_experiment_tracking.py tests/test_continuation_validation_guards.py -q && env/bin/python preflight.py'
subprocess.run(ssh+[cmd],check=True)
