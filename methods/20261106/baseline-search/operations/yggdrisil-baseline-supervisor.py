"""Bounded supervisor for the approved ten frozen baseline runs."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone

ROOT=Path('/home/ubuntu/yggdrisil-baselines-20261106')
BUNDLE=ROOT/'bundle'
APP=BUNDLE/'source/application'
sys.path[:0]=[str(APP),str(APP/'src'),str(BUNDLE/'source/framework/src')]
from scripts.collect_deletion_baselines import collect
from scripts.run_deletion_baseline import load_bundle, validate_approval
from yggdrisil_ecoli.baseline_storage import BaselineArchive,configured_secrets,screen,sqlite_snapshot
from yggdrisil_ecoli.data.io import atomic_json,file_sha256

read=lambda p:json.loads(p.read_bytes())
now=lambda:datetime.now(timezone.utc).isoformat()

def final_backup(state, archive, secrets, tracked, wandb):
    if any(p.poll() is None for p,log in CHILDREN.values()):
        raise RuntimeError('Cannot archive a live scientific graph')
    atomic_json(BUNDLE/'catalog.json',collect(BUNDLE))
    with tempfile.TemporaryDirectory(prefix='final-archive-',dir=ROOT) as scratch:
        temp=Path(scratch);data=temp/'data';data.mkdir()
        files=[]
        top=['protocol.json','manifest.json','source-manifest.json','jobs.json','inputs/starting-genotype.json','catalog.json','approval.json']
        sources=[(BUNDLE/n,n) for n in top]
        for name in ['batch-progress.json','preflight.json','input-verification.json','worker.json']:
            sources.append((ROOT/name,name))
        for job in JOBS:
            folder=BUNDLE/job['directory']
            for name in ['config.json','launch.json','tracking.json','progress.json','curves.json','result.json','failure.json','failures.jsonl','process.json','environment.json','completion.json','checkpoint.json','checkpoints.jsonl','controller.log']:
                if (folder/name).exists():sources.append((folder/name,job['directory']+'/'+name))
            if (folder/'graph.sqlite').exists():
                target=data/job['directory']/'graph.sqlite';target.parent.mkdir(parents=True,exist_ok=True)
                sqlite_snapshot(folder/'graph.sqlite',target)
        for source,name in sources:
            target=data/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(source.read_bytes())
        for p in sorted(data.rglob('*')):
            if p.is_file():
                screen(p,secrets);files.append({'path':str(p.relative_to(data)),'sha256':file_sha256(p),'bytes':p.stat().st_size})
        tarpath=temp/'data.tar.gz'
        with tarfile.open(tarpath,'w:gz') as tar:
            for f in files:tar.add(data/f['path'],arcname=f['path'],recursive=False)
        ref=archive.upload(tarpath,'final')
        index=temp/'index.json';atomic_json(index,{'experiment_id':SPEC['experiment_id'],'archive':ref,'files':files,'status':state['status']});screen(index,secrets)
        index_ref=archive.upload(index,'final')
        artifact=wandb.Artifact(SPEC['experiment_id']+'-final-data',type='experiment-data');artifact.add_file(str(tarpath),name='data.tar.gz');artifact.add_file(str(index),name='index.json');saved=tracked.log_artifact(artifact);saved.wait()
        verified=wandb.Api(timeout=120).artifact(saved.qualified_name)
        assert verified.state=='COMMITTED'
        with tempfile.TemporaryDirectory(prefix='final-verify-',dir=ROOT) as verification:
            base=Path(verified.download(root=verification))
            assert file_sha256(base/'data.tar.gz')==ref['sha256']
            assert file_sha256(base/'index.json')==index_ref['sha256']
        result={'status':'verified','verified_at':now(),'archive':ref,'index':index_ref,'members':len(files),'wandb_artifact':saved.qualified_name,'full_s3_get_verified':True,'wandb_download_verified':True}
        atomic_json(ROOT/'final-backup.json',result)
        return result

def stop_children():
    live=[p for p,log in CHILDREN.values() if p.poll() is None]
    for p in live:os.killpg(p.pid,signal.SIGINT)
    end=time.monotonic()+300
    for p in live:
        remaining=max(1,end-time.monotonic())
        try:p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            print('Controller failed to stop within five minutes; preserve its SQLite graph',p.pid,flush=True)
            os.killpg(p.pid,signal.SIGKILL);p.wait()

CHILDREN={}
if __name__=='__main__':
    if sys.platform!='linux':raise RuntimeError('Remote Linux only')
    lock=(ROOT/'batch.lock').open('a+');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if (ROOT/'batch-progress.json').exists():raise RuntimeError('Study already started; inspect process and preserved attempts before recovery')
    SPEC,_=load_bundle(BUNDLE,0);JOBS=read(BUNDLE/'jobs.json');approval=read(BUNDLE/'approval.json');validate_approval(BUNDLE,SPEC,approval)
    assert read(ROOT/'preflight.json')['status']=='passed'
    os.environ.update(read(ROOT/'credentials/tracking.json'))
    os.environ.update(PYTHONPATH=':'.join([str(BUNDLE/'source/framework/src'),str(APP/'src'),str(APP)]),OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',PYDANTIC_DISABLE_PLUGINS='__all__')
    import boto3,wandb
    secrets=configured_secrets(APP,ROOT/'credentials')
    state={'experiment_id':SPEC['experiment_id'],'status':'running','started_at':now(),'jobs':{},'approval':approval,'max_concurrent_jobs':4,'model_calls':0}
    def save():
        state['updated_at']=now();atomic_json(ROOT/'batch-progress.json',state)
    save()
    def interrupted(signum,frame):raise InterruptedError('Supervisor received signal '+str(signum))
    signal.signal(signal.SIGTERM,interrupted)
    with wandb.init(entity=SPEC['tracking']['entity'],project=SPEC['tracking']['project'],id='baseline-study-20261106',name=SPEC['experiment_id']+'-study',job_type='baseline',tags=['yggdrisil','baseline','study'],config={'protocol_sha256':file_sha256(BUNDLE/'protocol.json'),'approval':approval,'max_concurrent_jobs':4},dir=str(ROOT),settings=wandb.Settings(disable_git=True,disable_code=True,save_code=False)) as tracked:
        state['wandb_url']=tracked.url;save()
        archive=BaselineArchive(s3=boto3.client('s3',region_name='us-east-1'),wandb=wandb,tracked=tracked,owner=boto3.client('sts',region_name='us-east-1').get_caller_identity()['Account'],bucket=SPEC['tracking']['s3_bucket'],prefix=SPEC['tracking']['s3_prefix'],run_id='study',secrets=secrets)
        pending=list(range(len(JOBS)))
        try:
            while pending or any(p.poll() is None for p,log in CHILDREN.values()):
                if time.time()>=approval['science_deadline_unix']:raise TimeoutError('Original study science deadline reached')
                for i,(p,log) in list(CHILDREN.items()):
                    if p.poll() is not None and state['jobs'][str(i)]['status']=='running':
                        log.close();folder=BUNDLE/JOBS[i]['directory'];done=read(folder/'completion.json') if (folder/'completion.json').exists() else None
                        state['jobs'][str(i)].update(status='completed_verified' if p.returncode==0 and done and done['status']=='completed_verified' else 'failed',exit_code=p.returncode,finished_at=now(),completion=done)
                        save()
                        if (folder/'failure.json').exists() and read(folder/'failure.json')['phase']=='backup':raise RuntimeError('Job backup failed; preserve all worker data')
                live=sum(p.poll() is None for p,log in CHILDREN.values())
                while pending and live<4:
                    i=pending.pop(0);folder=BUNDLE/JOBS[i]['directory'];folder.mkdir(parents=True,exist_ok=True)
                    if (folder/'process.json').exists() or (folder/'graph.sqlite').exists():raise RuntimeError('Duplicate job dispatch refused')
                    log=(folder/'controller.log').open('xb');p=subprocess.Popen([str(ROOT/'env/bin/python'),'-u','-m','scripts.run_deletion_baseline','--bundle',str(BUNDLE),'--job-index',str(i),'--repo-root',str(APP),'--approval',str(BUNDLE/'approval.json')],cwd=APP,env=os.environ,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    CHILDREN[i]=(p,log);state['jobs'][str(i)]={'run_id':JOBS[i]['run_id'],'pid':p.pid,'start_ticks':Path(f'/proc/{p.pid}/stat').read_text().split()[21],'status':'running','started_at':now()};save();live+=1
                tracked.log({'jobs_started':len(CHILDREN),'jobs_running':live,'jobs_completed':sum(j['status']=='completed_verified' for j in state['jobs'].values())})
                time.sleep(15)
            # Collect the final exits even when all remaining children exit together.
            for i,(p,log) in CHILDREN.items():
                if not log.closed:log.close()
                folder=BUNDLE/JOBS[i]['directory'];done=read(folder/'completion.json') if (folder/'completion.json').exists() else None
                state['jobs'][str(i)].update(status='completed_verified' if p.returncode==0 and done and done['status']=='completed_verified' else 'failed',exit_code=p.returncode,finished_at=now(),completion=done)
            state['status']='search_completed' if all(j['status']=='completed_verified' for j in state['jobs'].values()) else 'completed_with_exceptions';save()
        except BaseException as exc:
            state.update(status='failed',error={'type':type(exc).__name__,'message':str(exc)});save();print(repr(exc),flush=True);stop_children()
        finally:
            stop_children()
            for p,log in CHILDREN.values():
                if not log.closed:log.close()
            save()
            try:
                state['final_backup']=final_backup(state,archive,secrets,tracked,wandb);save()
            except BaseException as exc:
                state['backup_failure']={'type':type(exc).__name__,'message':str(exc)};save();raise
            finally:
                subprocess.run(['sudo','shutdown','-h','+2'],check=True)
