import json,subprocess
from pathlib import Path
from datetime import datetime,timezone
r=Path('/home/ubuntu/yggdrisil-continuation-20261106');v=r/'validation'
assert json.loads((r/'validation-readiness.json').read_text())['status']=='passed'
assert not (v/'launcher.json').exists() and not (v/'batch-progress.json').exists()
log=(v/'supervisor.log').open('xb')
p=subprocess.Popen([str(r/'env/bin/python'),'-u',str(r/'yggdrisil-continuation-validation-supervisor.py')],cwd=r,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
record={'pid':p.pid,'start_ticks':Path(f'/proc/{p.pid}/stat').read_text().split()[21],'started_at':datetime.now(timezone.utc).isoformat(),'instance_id':'i-0512356f473be8f8d','waiting_for_search_completion':True}
(v/'launcher.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))
