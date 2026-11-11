import fcntl,hashlib,json,os,stat,subprocess,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path
from importlib.metadata import version
from urllib.parse import urlparse
ROOT=Path('/home/ubuntu/yggdrisil-baseline-depth-20261110');APP=ROOT/'application';BUNDLE=ROOT/'bundle'
sys.path[:0]=[str(ROOT/'framework/src'),str(APP/'src'),str(APP)]
from scripts.plan_search_replication import verify_bundle
from scripts.run_search_replication import worker_identity,save
from yggdrisil_ecoli.baseline_storage import BaselineArchive,configured_secrets,screen
from yggdrisil_ecoli.data.io import file_sha256
import boto3,yggdrisil,yggdrisil_ecoli
read=lambda p:json.loads(p.read_bytes())
assert sys.platform=='linux' and (ROOT/'runtime.ready').exists()
assert not (ROOT/'batch-progress.json').exists() and not (ROOT/'launcher.json').exists()
with (ROOT/'batch.lock').open('a+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
spec,jobs=verify_bundle(BUNDLE);worker=read(ROOT/'worker.json');approval=read(BUNDLE/'approval.json')
assert worker_identity()==worker['instance_id']==approval['instance_id']
for name,digest in read(BUNDLE/'runtime-manifest.json')['files'].items():
 if file_sha256(ROOT/name)!=digest:raise RuntimeError('Deployed source changed: '+name)
versions={name:version(name) for name in spec['required_versions']}
assert versions==spec['required_versions'],versions
for name,reference in spec['inputs'].items():assert file_sha256(APP/reference['path'])==reference['sha256'],name
assert read(BUNDLE/'input-verification.json')['status']=='verified'
assert Path(yggdrisil.__file__).resolve().is_relative_to(ROOT/'framework/src')
assert Path(yggdrisil_ecoli.__file__).resolve().is_relative_to(APP/'src')
from scripts.baseline_depth_extension import validate_approval
validate_approval(approval)
assert approval['science_dispatch_ready'] is False
stop=datetime.fromtimestamp(approval['stop_deadline_unix'],timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
timer=subprocess.check_output(['systemctl','show','yggdrisil-depth-stop.timer','--property=ActiveState','--property=NextElapseUSecRealtime'],text=True)
assert 'ActiveState=active' in timer and stop in timer,timer
for p in Path('/proc').glob('[0-9]*/cmdline'):
 try:args=p.read_bytes().split(bytes([0]))
 except FileNotFoundError:continue
 if any(x in args for x in [b'scripts.supervise_search_replication',b'scripts.run_search_replication']):raise RuntimeError('Controller already exists')
env={**os.environ,'PYTHONPATH':':'.join([str(ROOT/'framework/src'),str(APP/'src'),str(APP)]),'PYTHONDONTWRITEBYTECODE':'1'}
tests=subprocess.run([sys.executable,'-m','pytest','-q','tests/test_baseline_depth_extension.py','tests/test_search_replication.py','tests/test_replication_launch.py','tests/test_replication_backups.py'],cwd=APP,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
(ROOT/'preflight-tests.log').write_text(tests.stdout)
if tests.returncode:raise RuntimeError('Offline checks failed; inspect preflight-tests.log')
report={'status':'checking_backups','checked_at':datetime.now(timezone.utc).isoformat(),'instance_id':worker['instance_id'],'protocol_sha256':file_sha256(BUNDLE/'protocol.json'),'runtime_manifest_sha256':file_sha256(BUNDLE/'runtime-manifest.json'),'source_verification':'all deployed files matched','input_verification_sha256':file_sha256(BUNDLE/'input-verification.json'),'versions':versions,'timer':timer,'offline_tests_log_sha256':file_sha256(ROOT/'preflight-tests.log'),'tests_summary':tests.stdout.strip().splitlines()[-3:],'local_scientific_execution':False,'scientific_dispatch_started':False}
credentials=ROOT/'credentials/tracking.json'
assert stat.S_IMODE(credentials.stat().st_mode)==0o600
login=read(credentials);assert set(login)<={'WANDB_API_KEY','WANDB_BASE_URL'} and login['WANDB_API_KEY']
os.environ.update(login)
secrets=configured_secrets(APP,ROOT/'credentials')
path=ROOT/'backup-preflight-probe.json';save(path,report);screen(path,secrets)
location=urlparse(spec['tracking']['s3_prefix']);s3=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts').get_caller_identity()['Account']
store=BaselineArchive(s3=s3,wandb=None,tracked=None,owner=owner,bucket=location.netloc,prefix=location.path.lstrip('/'),run_id='preflight',secrets=secrets)
ref=store.upload(path,'probe');report['s3_probe']=ref
import wandb
with wandb.init(entity='mcclain',project='yggdrisil',id=spec['experiment_id']+'-preflight',name=spec['experiment_id']+'-preflight',job_type='preflight',dir=str(ROOT),settings=wandb.Settings(disable_git=True,disable_code=True,save_code=False)) as run:
 artifact=wandb.Artifact(spec['experiment_id']+'-preflight-data',type='experiment-data');artifact.add_file(str(path),name=path.name)
 logged=run.log_artifact(artifact);logged.wait();remote=wandb.Api(timeout=120).artifact(logged.qualified_name)
 assert remote.state=='COMMITTED'
 with tempfile.TemporaryDirectory(prefix='preflight-verify-',dir=ROOT) as temp:
  downloaded=Path(remote.get_entry(path.name).download(root=temp,skip_cache=True));assert file_sha256(downloaded)==ref['sha256'] and downloaded.stat().st_size==ref['bytes']
 report.update(status='passed',s3_full_get_verified=True,wandb_verified=True,wandb_uncached_download=True,wandb_artifact=logged.qualified_name,wandb_url=run.url)
 save(ROOT/'preflight.json',report)
print(json.dumps({'status':report['status'],'s3_probe':ref,'wandb_artifact':report['wandb_artifact']}))
