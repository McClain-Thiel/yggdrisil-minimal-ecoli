import fcntl,json,os,pathlib,subprocess,time
r=pathlib.Path('/home/ubuntu/yggdrisil-baseline-depth-20261110')
launch_lock=(r/'launch.lock').open('a+')
fcntl.flock(launch_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
assert not (r/'batch-progress.json').exists() and not (r/'launcher.json').exists()
assert json.loads((r/'preflight.json').read_text())['status']=='passed'
with (r/'batch.lock').open('a+') as lock:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
for p in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
 try:cmd=p.read_bytes()
 except FileNotFoundError:continue
 if b'scripts.supervise_search_replication' in cmd and b'--root' in cmd and p.parts[2]!=str(os.getpid()):
  # Ignore the parent SSH shell containing this launch snippet; actual Python module argv has exact fields.
  args=cmd.split(bytes([0]))
  if b'scripts.supervise_search_replication' in args:raise RuntimeError('Existing supervisor found')
env={**os.environ,'PYTHONPATH':':'.join([str(r/'framework/src'),str(r/'application/src'),str(r/'application')]),'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1','MKL_NUM_THREADS':'1','PYDANTIC_DISABLE_PLUGINS':'__all__'}
log=(r/'supervisor.log').open('xb')
p=subprocess.Popen([str(r/'env/bin/python'),'-u','-m','scripts.supervise_search_replication','--root',str(r)],cwd=r/'application',env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
info={'pid':p.pid,'start_ticks':pathlib.Path(f'/proc/{p.pid}/stat').read_text().split()[21],'started_at_unix':time.time(),'instance_id':'i-05b12ad155a4aff74','remote_root':str(r)}
(r/'launcher.json').write_text(json.dumps(info,indent=2)+'\n')
print(json.dumps(info))
