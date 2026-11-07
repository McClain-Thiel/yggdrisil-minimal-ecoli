"""Read live metadata and independently verify immutable S3/W&B checkpoints."""
import fcntl,hashlib,json,math,os,shutil,subprocess,sys,tempfile
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')
sys.path[:0]=[str(ROOT/'application/src'),str(ROOT/'application'),str(ROOT/'framework/src')]
from yggdrisil_ecoli.baseline_storage import configured_secrets,screen
from yggdrisil_ecoli.data.io import atomic_json,file_sha256
import boto3,wandb
read=lambda p:json.loads(p.read_bytes())
now=lambda:datetime.now(timezone.utc).isoformat()
os.environ.update(read(ROOT/'credentials/tracking.json'))
secrets=configured_secrets(ROOT/'application',ROOT/'credentials')
s3=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts').get_caller_identity()['Account'];api=wandb.Api(timeout=120)
cache_dir=ROOT/'.verification';cache_dir.mkdir(exist_ok=True)
cache_path=cache_dir/'cache.json';cache=read(cache_path) if cache_path.exists() else {}

def fetch(ref,retain=False):
    u=urlparse(ref['uri']);h=hashlib.sha256();count=0;parts=[]
    response=s3.get_object(Bucket=u.netloc,Key=u.path.lstrip('/'),ExpectedBucketOwner=owner)['Body']
    try:
        for chunk in response.iter_chunks(chunk_size=1024**2):
            h.update(chunk);count+=len(chunk)
            if retain:parts.append(chunk)
    finally:response.close()
    if h.hexdigest()!=ref['sha256'] or count!=ref['bytes']:raise RuntimeError('S3 content mismatch: '+ref['uri'])
    return b''.join(parts) if retain else None

summary={'checked_at':now(),'batch':read(ROOT/'batch-progress.json'),'jobs':{}}
summary['status']=summary['batch']['status']
for name,digest in read(ROOT/'deployment-manifest.json')['files'].items():
    if file_sha256(ROOT/name)!=digest:raise RuntimeError('Frozen deployed source/input changed: '+name)
summary['source_files_verified']=True
assert file_sha256(ROOT/'native-validated/wholecell/utils/_netflow/nf_glpk.py')=='5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686'
with (ROOT/'batch.lock').open() as lock:
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:summary['supervisor_lock']='held'
    else:
        summary['supervisor_lock']='free';fcntl.flock(lock,fcntl.LOCK_UN)
launcher=read(ROOT/'launcher.json');proc=Path('/proc')/str(launcher['pid'])
summary['supervisor_alive']=proc.exists() and (proc/'stat').read_text().split()[21]==launcher['start_ticks']
processes=subprocess.check_output(['ps','-eo','pid,ppid,etimes,pcpu,rss,args'],text=True).splitlines()[1:]
summary['cell_processes']=[line for line in processes if 'ecoli_master_sim.py' in line and str(ROOT) in line and 'python' in line]
summary['running_cells']=len(summary['cell_processes']);summary['free_disk_gib']=shutil.disk_usage(ROOT).free/1024**3
summary['timer']=subprocess.check_output(['systemctl','show','yggdrisil-baseline-validation-stop.timer','--property=ActiveState','--property=NextElapseUSecRealtime'],text=True)
summary['supervisor_log_tail']=(ROOT/'supervisor.log').read_text()[-1500:]
for label in ['calibration','seed-101','seed-211','seed-307']:
    folder=ROOT/'runs'/label;entry=read(folder/'experiments.json')['experiments'][0]
    job={'catalog':entry,'status':entry['status'],'manifest_sha256':file_sha256(folder/'manifest.json')}
    if (folder/'process.json').exists():
        job['process']=read(folder/'process.json')
        job['controller_alive']=Path('/proc',str(job['process']['controller_pid'])).exists()
    backup=entry.get('data_backup',{})
    if backup.get('s3_checkpoint'):
        index=json.loads(fetch(backup['s3_checkpoint'],True));artifact=api.artifact(backup['wandb_artifact'])
        if artifact.state!='COMMITTED':raise RuntimeError('Artifact not committed: '+label)
        verified=0;reused=0
        with tempfile.TemporaryDirectory(prefix='files-',dir=cache_dir) as temporary:
            for name,ref in index['files'].items():
                artifact_entry=artifact.get_entry(name);key=ref['uri'];prior=cache.get(key)
                if prior and prior['sha256']==ref['sha256'] and prior['wandb_digest']==artifact_entry.digest:
                    reused+=1;continue
                fetch(ref)
                downloaded=Path(artifact_entry.download(root=temporary))
                if file_sha256(downloaded)!=ref['sha256'] or downloaded.stat().st_size!=ref['bytes']:
                    raise RuntimeError('W&B content mismatch: '+label+'/'+name)
                cache[key]={'sha256':ref['sha256'],'bytes':ref['bytes'],'wandb_digest':artifact_entry.digest,'verified_at':now()}
                verified+=1
        job.update(artifact_state=artifact.state,verified_now=now(),all_checkpoint_objects_verified=True,objects_downloaded=verified,unchanged_verified_objects=reused,checkpoint_members=len(index['files']))
        if 'progress.json' in index['files']:
            data=fetch(index['files']['progress.json'],True)
            if hashlib.sha256(data).hexdigest()!=backup['progress_sha256']:raise RuntimeError('Published progress differs: '+label)
            progress=json.loads(data)
            assert progress['manifest_sha256']==job['manifest_sha256']
            for finalist in progress['finalists']:
                for generation in finalist['generations']:
                    for key in ['generation_duration_s','final_cell_mass_fg','final_dry_mass_fg','final_dry_mass_fold_change']:
                        assert math.isfinite(generation[key]),(label,key)
            job['verified_progress']=progress
    for name in ['progress.json','results.json']:
        if (folder/name).exists():job[name]=read(folder/name)
    for name in ['controller.log','workflow.log','events.jsonl']:
        if (folder/name).exists():job[name+'_tail']=(folder/name).read_text()[-1500:]
    summary['jobs'][label]=job
summary['verification_completed_at']=now()
atomic_json(cache_path,cache)
target=ROOT/'monitoring-summary.json';atomic_json(target,summary);screen(target,secrets)
print(json.dumps({'status':summary['status'],'supervisor_alive':summary['supervisor_alive'],'cells':summary['running_cells'],'jobs':{name:{'status':j['status'],'saved_cycles':j['catalog'].get('data_backup',{}).get('completed_cell_cycles',0),'verified':j.get('all_checkpoint_objects_verified',False)} for name,j in summary['jobs'].items()}}))
