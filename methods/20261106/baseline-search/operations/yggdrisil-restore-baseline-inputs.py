import hashlib,json,shutil,tarfile
from pathlib import Path
from urllib.parse import urlparse
import boto3
r=Path('/home/ubuntu/yggdrisil-baselines-20261106');s3=boto3.client('s3',region_name='us-east-1')
plan=json.loads((r/'restore-plan.json').read_bytes());cache=r/'input-archives';cache.mkdir(exist_ok=True)
sha=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
for item in plan:
 ref=item['archive'];p=cache/(ref['sha256']+'.tar.gz');u=urlparse(ref['uri'])
 if not p.exists():s3.download_file(u.netloc,u.path.lstrip('/'),str(p))
 if sha(p)!=ref['sha256']:raise ValueError('Archive checksum mismatch')
 name=Path(item['path']);assert not name.is_absolute() and '..' not in name.parts and name.parts[0]=='data'
 target=r/'bundle/source/application'/name
 with tarfile.open(p) as tar:
  member=tar.getmember(str(name));assert member.isfile();target.parent.mkdir(parents=True,exist_ok=True)
  with tar.extractfile(member) as source,target.open('wb') as out:shutil.copyfileobj(source,out)
 assert sha(target)==item['sha256'],name
report={'status':'verified','files':len(plan),'input_archive_refs':list({x['archive']['sha256']:x['archive'] for x in plan}.values()),'restored':[{k:x[k] for k in ('path','sha256')} for x in plan]}
(r/'input-verification.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({'status':'verified','files':len(plan)}))
