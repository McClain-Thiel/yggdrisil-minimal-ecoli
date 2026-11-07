import fcntl,json,os,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
r=Path('/home/ubuntu/yggdrisil-baselines-20261106');b=r/'bundle';app=b/'source/application'
assert json.loads((r/'preflight.json').read_bytes())['status']=='passed'
assert '2 supervisor archival checks passed' in (r/'supervisor-tests.log').read_text()
assert not (r/'launcher.json').exists() and not (r/'batch-progress.json').exists()
with (r/'batch.lock').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(lock,fcntl.LOCK_UN)
env={**os.environ,'PYTHONPATH':':'.join([str(b/'source/framework/src'),str(app/'src'),str(app)]),'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','PYDANTIC_DISABLE_PLUGINS':'__all__'}
log=(r/'supervisor.log').open('xb');p=subprocess.Popen([str(r/'env/bin/python'),'-u',str(r/'yggdrisil-baseline-supervisor.py')],cwd=r,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
info={'pid':p.pid,'start_ticks':Path(f'/proc/{p.pid}/stat').read_text().split()[21],'started_at':datetime.now(timezone.utc).isoformat(),'instance_id':json.loads((b/'approval.json').read_bytes())['instance_id']};(r/'launcher.json').write_text(json.dumps(info,indent=2)+'\n');time.sleep(2)
if p.poll() is not None:raise RuntimeError('Supervisor exited during startup; inspect preserved log, never duplicate blindly')
print(json.dumps(info))
