"""Resume the verified frozen validation after a pre-simulation backup setup failure."""
import fcntl,hashlib,importlib.util,json,os,signal,subprocess,sys,time
from pathlib import Path
R=Path('/home/ubuntu/yggdrisil-replication-20261109');V=R/'validation';B=R/'bundle';H=R/'operations/resume-dispatch-attempt2'
sys.path[:0]=[str(V/'application/src'),str(R/'application'),str(R/'application/src'),str(R/'framework/src')]
from scripts.run_search_replication import save,validate_launch
from scripts.supervise_search_replication import publish
from yggdrisil_ecoli.baseline_storage import BaselineArchive,configured_secrets
import wandb
read=lambda p:json.loads(p.read_bytes());sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
def identity(pid):return {'pid':pid,'start_ticks':Path(f'/proc/{pid}/stat').read_text().split()[21]}
def main():
 if sys.platform!='linux':raise RuntimeError('Remote Linux only')
 plan=read(V/'parallel-plan.json');approval=read(B/'approval.json');validate_launch(B,R/'application',approval)
 assert time.time()<plan['science_deadline_unix'] and sha(Path(__file__))==read(R/'validation-handoff-recovery5.json')['supervisor_sha256']
 lock=(R/'batch.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 with (V/'batch.lock').open('a+') as vlock:fcntl.flock(vlock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 assert not (V/'batch-progress.json').exists() and not (V/'supervisor.log').exists()
 preserved=read(H/'preservation.json');assert preserved['completed_scientific_work_preserved']
 assert read(V/'resume-backup-preflight.json')['status']=='passed'
 old=read(R/'batch-progress.json');assert old['status']=='failed_preserving_disk'
 frozen=sha(B/'nominations.json');assert frozen=='ddb1eb0233e5a918c402db26bcb8c1d2f4844b850dfde337b56f92af4892b1a5'
 os.environ.update(read(R/'credentials/tracking.json'))
 for name in ['OPENROUTER_API_KEY','EDISON_PLATFORM_API_KEY']:os.environ.pop(name,None)
 spec=importlib.util.spec_from_file_location('parallel',V/'supervisor.py');parallel=importlib.util.module_from_spec(spec);spec.loader.exec_module(parallel);parallel.activate_paths()
 s3,owner=parallel.client();secrets=configured_secrets(V/'application',R/'credentials')
 state={**old,'status':'validation_preparing','supervisor':identity(os.getpid()),'handoff_recovery5':{'preservation':preserved,'scientific_inputs_unchanged':True,'nominations_sha256':frozen,'searches_relaunched':0,'controller_backup_preflight':read(V/'resume-backup-preflight.json')},'error':None}
 # A preserved setup-only final archive is historical evidence, not the current result.
 for key in ['validation_final_backup','final_catalog_backup']:state.pop(key,None)
 def persist():state['updated_at_unix']=time.time();save(R/'batch-progress.json',state)
 def interrupted(signum,frame):raise InterruptedError('Recovery supervisor received signal '+str(signum))
 signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
 process=None
 with wandb.init(entity='mcclain',project='yggdrisil',id='search-replication-20261109-validation-handoff-recovery5',name='search-replication-20261109-validation-handoff-recovery5',job_type='operational-recovery',tags=['yggdrisil','validation','recovery'],config={'science_unchanged':True,'searches_relaunched':0,'plan_sha256':sha(V/'parallel-plan.json')},dir=str(R),settings=wandb.Settings(disable_git=True,disable_code=True,save_code=False)) as tracked:
  state['wandb_url']=tracked.url;persist();save(R/'launcher.json',{**identity(os.getpid()),'script':str(Path(__file__)),'started_at_unix':time.time(),'recovery':'verified-checkpoint-tracking-resume'})
  archive=BaselineArchive(s3=s3,wandb=wandb,tracked=tracked,owner=owner,bucket='phd-research-storage-1758274488',prefix='yggdrisil/experiments/search-replication-20261109/',run_id='validation-handoff-recovery5',secrets=secrets)
  try:
   state['handoff_recovery5']['before_retry_backup']=publish({'preservation.json':H/'preservation.json','resume-backup-preflight.json':V/'resume-backup-preflight.json','parallel-plan.json':V/'parallel-plan.json','recovery.json':R/'validation-handoff-recovery5.json'},archive,tracked,wandb,'before-retry',secrets);persist()
   parallel.prepare_assignments(plan)
   env={**os.environ,'PYTHONPATH':':'.join([str(V/'application/src'),str(R/'application'),str(R/'application/src'),str(R/'framework/src')]),'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','PYDANTIC_DISABLE_PLUGINS':'__all__'}
   log=(V/'supervisor.log').open('xb');process=subprocess.Popen([sys.executable,'-u',str(V/'supervisor.py')],cwd=V,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
   state.update(status='validation_running',validation_process=identity(process.pid));persist()
   while process.poll() is None:
    if time.time()>=plan['science_deadline_unix']:raise TimeoutError('Original parallel science deadline reached')
    time.sleep(15)
   log.close()
   if process.returncode!=0 or not (V/'final-backup.json').exists():raise RuntimeError('Validation coordinator ended without verified final backup')
   final=read(V/'final-backup.json');assert final['status']=='verified' and final['all_shards_independently_verified']
   state['validation_final_backup']=final;persist()
   results=read(V/'parallel-results.json')
   if any(w['study']['status']=='failed' for w in results['workers'].values()):raise RuntimeError('A validation worker failed operationally; outputs preserved, inspect worker catalogs')
   state['status']='completed_pending_independent_analysis';persist()
   state['final_catalog_backup']=publish({'batch-progress.json':R/'batch-progress.json','nominations.json':B/'nominations.json','validation/batch-progress.json':V/'batch-progress.json','validation/final-backup.json':V/'final-backup.json','validation/parallel-results.json':V/'parallel-results.json'},archive,tracked,wandb,'final-catalog',secrets);persist()
  except BaseException as error:
   state.update(status='failed_preserving_disk',error={'type':type(error).__name__,'message':str(error)});persist()
   if process is not None and process.poll() is None:process.send_signal(signal.SIGTERM);process.wait(timeout=max(1,plan['stop_deadline_unix']-time.time()-60))
   state['handoff_recovery_failure_backup']=publish({'batch-progress.json':R/'batch-progress.json','nominations.json':B/'nominations.json'},archive,tracked,wandb,'failure',secrets);persist();raise
  finally:subprocess.run(['sudo','shutdown','-h','+2'],check=True)
if __name__=='__main__':main()
