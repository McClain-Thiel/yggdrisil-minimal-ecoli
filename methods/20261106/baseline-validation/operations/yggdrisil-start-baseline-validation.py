import json,subprocess
from pathlib import Path
ssh=['ssh','-i',str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),'-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ClearAllForwardings=yes','ubuntu@52.87.179.109']
remote=r'''
import fcntl,json,os,subprocess,time
from pathlib import Path
from datetime import datetime,timezone
root=Path('/home/ubuntu/yggdrisil-baseline-validation-20261106')
lock=(root/'batch.lock').open('a+')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert not (root/'batch-progress.json').exists()
assert not (root/'launcher.json').exists()
assert json.loads((root/'preflight.json').read_bytes())['status']=='passed'
for p in Path('/proc').iterdir():
 if p.name.isdigit():
  try:cmd=(p/'cmdline').read_bytes().split(b'\0')
  except FileNotFoundError:continue
  if len(cmd)>1 and cmd[1].endswith(b'/supervisor.py') and str(root).encode() in cmd[1]:
   raise RuntimeError('An existing supervisor is running')
fcntl.flock(lock,fcntl.LOCK_UN)
log=(root/'supervisor.log').open('xb')
env={**os.environ,'PYTHONPATH':':'.join([str(root/'application/src'),str(root/'application'),str(root/'framework/src')]),'PYDANTIC_DISABLE_PLUGINS':'__all__'}
process=subprocess.Popen([str(root/'env/bin/python'),'-u',str(root/'supervisor.py')],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True)
record={'pid':process.pid,'start_ticks':(Path('/proc')/str(process.pid)/'stat').read_text().split()[21],'started_at':datetime.now(timezone.utc).isoformat(),'command':[str(root/'env/bin/python'),'-u',str(root/'supervisor.py')],'preflight_status':'passed','unique_launch':True}
(root/'launcher.json').write_text(json.dumps(record,indent=2)+'\n')
time.sleep(3)
if process.poll() is not None:raise RuntimeError('Supervisor exited at launch; inspect supervisor.log')
print(json.dumps(record))
'''
subprocess.run(ssh+['python3 -'],input=remote.encode(),check=True)
