import json,subprocess
from pathlib import Path
from datetime import datetime,timezone
r=Path('/home/ubuntu/yggdrisil-continuation-20261106');assert not (r/'search-launcher.json').exists();assert not (r/'search-progress.json').exists();assert json.loads((r/'preflight.json').read_text())['status']=='passed'
assert '22 passed' in (r/'search-tests-approved.log').read_text()
log=(r/'search-controller.log').open('xb');p=subprocess.Popen([str(r/'env/bin/python'),'-u',str(r/'yggdrisil-continuation-controller.py')],cwd=r,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
info={'pid':p.pid,'start_ticks':Path(f'/proc/{p.pid}/stat').read_text().split()[21],'started_at':datetime.now(timezone.utc).isoformat(),'instance_id':'i-0512356f473be8f8d'};(r/'search-launcher.json').write_text(json.dumps(info,indent=2)+'\n');print(json.dumps(info))
