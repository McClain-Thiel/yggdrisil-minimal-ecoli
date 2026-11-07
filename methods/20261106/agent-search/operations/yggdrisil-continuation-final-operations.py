import json,hashlib,subprocess,fcntl
from pathlib import Path
from datetime import datetime,timezone
r=Path('/home/ubuntu/yggdrisil-continuation-20261106')
secrets=[v for p in (r/'credentials').glob('*.json') for k,v in json.loads(p.read_text()).items() if k.endswith('_KEY') and isinstance(v,str)]
names=['source-snapshot.json','validation-readiness.json','protocol.json','deadlines.json','search-progress.json','search-ended.json','search-launcher.json','validation/launcher.json','validation/batch-progress.json','validation/final-backup.json','validation/supervisor.log','analysis-verification-attempt1.log','analysis-verification.log','backup-verification-attempt1.log']
d={'experiment_id':'vecoli-kimi-continuation-20gen-3seed-20261106','captured_at':datetime.now(timezone.utc).isoformat(),'files':{}}
for n in names:
 p=r/n;b=p.read_bytes();d['files'][n]={'sha256':hashlib.sha256(b).hexdigest(),'bytes':len(b),'json':json.loads(b) if n.endswith('.json') else None,'text':b.decode() if not n.endswith('.json') else None}
d['audit_source_sha256']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in r.glob('yggdrisil-continuation-final-*.py')}
rows=subprocess.check_output(['ps','-eo','pid,comm,args'],text=True).splitlines()
d['scientific_processes']=[x for x in rows if any(v in x for v in ['ecoli_master_sim.py','track_vecoli_experiment.py','run-controller.py','yggdrisil-continuation-controller.py','yggdrisil-continuation-validation-supervisor.py']) and 'python -' not in x]
assert not d['scientific_processes'],d['scientific_processes']
with (r/'validation/batch.lock').open() as f:
 fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);fcntl.flock(f,fcntl.LOCK_UN)
d['batch_lock']='free';d['absolute_stop_timer']=subprocess.check_output(['systemctl','list-timers','yggdrisil-budget-deadline.timer','--no-pager'],text=True)
assert '2026-11-06 19:49:57' in d['absolute_stop_timer']
encoded=json.dumps(d,indent=2)+'\n';assert not any(s in encoded for s in secrets);(r/'operational-final-record.json').write_text(encoded);print(encoded)
