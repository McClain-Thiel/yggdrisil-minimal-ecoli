import hashlib,json,os,stat,subprocess,sys
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')
sys.path[:0]=[str(ROOT/'application/src'),str(ROOT/'application'),str(ROOT/'framework/src')]
from yggdrisil_ecoli.baseline_storage import BaselineArchive,configured_secrets,screen
from yggdrisil_ecoli.data.io import atomic_json,file_sha256
read=lambda p:json.loads(p.read_bytes())
assert sys.platform=='linux'
assert not (ROOT/'batch-progress.json').exists()
assert read(ROOT/'prepared.json')['status']=='passed'
spec=read(ROOT/'protocol.json');auth=read(ROOT/'approval.json')
assert auth['paid_launch'] and auth['total_cap_usd']==30
for name,digest in read(ROOT/'deployment-manifest.json')['files'].items():
    assert file_sha256(ROOT/name)==digest,name
credentials=ROOT/'credentials/tracking.json'
assert stat.S_IMODE(credentials.stat().st_mode)==0o600
login=read(credentials)
assert set(login)<={'WANDB_API_KEY','WANDB_BASE_URL'} and login['WANDB_API_KEY']
os.environ.update(login)
timer=subprocess.check_output(['systemctl','show','yggdrisil-baseline-validation-stop.timer','--property=ActiveState','--property=NextElapseUSecRealtime'],text=True)
assert 'ActiveState=active' in timer and '2026-11-07 17:19:39' in timer,timer
assert subprocess.check_output(['git','-C',str(ROOT/'native'),'rev-parse','HEAD'],text=True).strip()==spec['native_runtime']['git_commit']
assert not subprocess.check_output(['git','-C',str(ROOT/'native'),'status','--porcelain','--untracked-files=no'],text=True).strip()
patch_status=subprocess.check_output(['git','-C',str(ROOT/'native-validated'),'status','--porcelain','--untracked-files=no'],text=True).strip()
assert patch_status=='M wholecell/utils/_netflow/nf_glpk.py',patch_status
assert file_sha256(ROOT/'native-validated/wholecell/utils/_netflow/nf_glpk.py')=='5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686'
import boto3,wandb
secrets=configured_secrets(ROOT/'application',ROOT/'credentials')
report={'status':'checking_backups','checked_at':datetime.now(timezone.utc).isoformat(),
 'experiment_id':spec['experiment_id'],'exact_genotypes':len(read(ROOT/'prepared.json')['genotypes']),
 'knockout_audit_sha256':file_sha256(ROOT/'knockout-audit/variant-knockout-audit.json'),
 'source_manifest_sha256':file_sha256(ROOT/'deployment-manifest.json'),'timer':timer,
 'credential_permissions':'owner-only; W&B only','native_commit':spec['native_runtime']['git_commit'],
 'native_solver_patch_sha256':file_sha256(ROOT/'solver-recovery-v2.patch'),
 'controller_packages':subprocess.check_output([str(ROOT/'env/bin/uv'),'pip','freeze','--python',str(ROOT/'env/bin/python')],text=True).splitlines(),
 'native_extensions':{str(p.relative_to(ROOT/'native')):file_sha256(p) for p in (ROOT/'native/wholecell/utils').glob('*.so')},
 'tests':{'controller_and_solver_guards':12,'launch_guards':2,'protein_pattern_scanner_guards':4,'solver':7,'solver_captured_fixture':'Historical captured-LP test retained in prior verification; not available on this worker. No runtime criterion changed.'}}
path=ROOT/'backup-preflight-probe.json';atomic_json(path,report);screen(path,secrets)
with wandb.init(entity='mcclain',project='yggdrisil',id='vecoli-baselines-preflight-20261106-retry1',name=spec['experiment_id']+'-preflight',job_type='validation-preflight',tags=['yggdrisil','baseline','validation'],dir=str(ROOT),settings=wandb.Settings(disable_git=True,disable_code=True,save_code=False)) as run:
    store=BaselineArchive(s3=boto3.client('s3',region_name='us-east-1'),wandb=wandb,tracked=run,
      owner=boto3.client('sts').get_caller_identity()['Account'],bucket=spec['tracking']['s3_bucket'],prefix=spec['tracking']['s3_prefix'],run_id='study',secrets=secrets)
    ref=store.upload(path,'preflight')
    artifact=wandb.Artifact(spec['experiment_id']+'-preflight-data',type='experiment-data');artifact.add_file(str(path),name=path.name)
    logged=run.log_artifact(artifact);logged.wait()
    remote=wandb.Api(timeout=120).artifact(logged.qualified_name);assert remote.state=='COMMITTED'
    downloaded=Path(remote.get_entry(path.name).download(root=str(ROOT/'preflight-verify')))
    assert file_sha256(downloaded)==ref['sha256']
    report.update(status='passed',backup_test={'s3':ref,'wandb_artifact':logged.qualified_name,'wandb_url':run.url,'full_download_verified':True})
    atomic_json(ROOT/'preflight.json',report)
print('Preflight passed with exact genotype audit and independently downloaded S3/W&B probe.')
