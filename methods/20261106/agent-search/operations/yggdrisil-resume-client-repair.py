import fcntl,hashlib,json,shutil,subprocess
from pathlib import Path
from datetime import datetime,timezone
r=Path('/home/ubuntu/yggdrisil-continuation-20261106')
lock=(r/'search-controller.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
ended=json.loads((r/'search-ended.json').read_text());assert ended['all_model_children_stopped']
for j in ended['jobs'].values():
 p=Path(f'/proc/{j["pid"]}/stat');assert not p.exists() or p.read_text().split()[2]=='Z'
a=r/'attempts/missing-client-dependency';a.mkdir(exist_ok=False)
for name in ['search-launcher.json','search-progress.json','search-ended.json','latest-search-checkpoint.json','search-controller.log','source-snapshot.json','preflight.json','search-0.log','search-1.log','search-2.log']:
 (r/name).rename(a/name)
for d in (r/'application/runs/literature-ablation-kimi-continuation-20261106/search').iterdir():
 target=a/d.name;target.mkdir()
 for n in ['config.json','launch.json','progress.json','failure.json','tracking.json','artifact.json']:
  if (d/n).exists():shutil.copy2(d/n,target/n)
(r/'client-repair.json').write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),'reason':'Missing optional MCP client dependency blocked first API request; no billed usage','added_dependency':'mcp==2.3.0','resume':'Same cloned checkpoint, key, execution identity and original stage deadline; --resume','preserved_checkpoint':ended['latest_checkpoint']},indent=2)+'\n')
fcntl.flock(lock,fcntl.LOCK_UN)
