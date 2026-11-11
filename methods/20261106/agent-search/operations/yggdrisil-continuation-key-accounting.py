"""Reconcile only this continuation's scoped model keys; management stays local."""
import argparse,json,subprocess
from datetime import datetime,timezone
from pathlib import Path
import httpx
root=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');folder=root/'runs/literature-ablation-kimi-continuation-20261106'
parser=argparse.ArgumentParser();parser.add_argument('--disable-after-stop',action='store_true');args=parser.parse_args()
if args.disable_after_stop:
 code='''import json,pathlib
r=pathlib.Path("/home/ubuntu/yggdrisil-continuation-20261106")
s=json.loads((r/"search-ended.json").read_text());assert s["all_model_children_stopped"]
for j in s["jobs"].values():
 p=pathlib.Path(f"/proc/{j['pid']}/stat")
 assert not p.exists() or p.read_text().split()[2]=="Z"
print("all_model_children_stopped")'''
 import shlex
 cmd=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','-o','ClearAllForwardings=yes','-o','HostKeyAlias=54.163.102.65','-i',str(Path.home()/'.ssh/mcclain-phd-aws-dev.pem'),'ubuntu@34.227.172.82','python3 -c '+shlex.quote(code)]
 assert subprocess.check_output(cmd,text=True).strip()=='all_model_children_stopped'
management=json.loads((root/'runs/literature-ablation-deployment-20261103/credentials/management.json').read_text())['OPENROUTER_MANAGEMENT_KEY']
rows=[]
with httpx.Client(base_url='https://openrouter.ai/api/v1',headers={'Authorization':'Bearer '+management},timeout=30) as client:
 for record in json.loads((folder/'key-metadata.json').read_text()):
  assert record['name'].startswith(folder.name+'-') and record['limit']==12
  path='/keys/'+record['hash']
  if args.disable_after_stop:
   response=client.patch(path,json={'disabled':True});response.raise_for_status()
  response=client.get(path);response.raise_for_status();data=response.json()['data']
  assert data['limit']==12
  if args.disable_after_stop:assert data['disabled']
  rows.append({k:data[k] for k in ['name','hash','limit','usage','disabled']})
result={'checked_at':datetime.now(timezone.utc).isoformat(),'keys':rows,'total_model_spend_usd':sum(x['usage'] for x in rows),'combined_cap_usd':36,'settlement_note':'Reconcile again after settlement; current provider usage is not a final bill.','all_disabled':all(x['disabled'] for x in rows)}
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
for p in [folder/'provider-accounting.json',folder/f'provider-accounting-{stamp}.json']:p.write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
