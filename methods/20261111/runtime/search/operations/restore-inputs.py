"""Restore verified scientific inputs on Linux, without restoring agent memory."""
import hashlib,json,shutil,sqlite3,sys,tarfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
import boto3

if sys.platform != 'linux':raise RuntimeError('Scientific input restoration is remote-only')
ROOT=Path('/home/ubuntu/yggdrisil-baseline-depth-20261110');BUNDLE=ROOT/'bundle'
read=lambda p:json.loads(p.read_bytes())
sha=lambda p:hashlib.file_digest(p.open('rb'),'sha256').hexdigest()
spec=read(BUNDLE/'protocol.json');cache=ROOT/'input-archives';cache.mkdir(exist_ok=True)
s3=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts').get_caller_identity()['Account']
s3.head_bucket(Bucket='phd-research-storage-1758274488',ExpectedBucketOwner=owner)
assert all(s3.get_public_access_block(Bucket='phd-research-storage-1758274488',ExpectedBucketOwner=owner)['PublicAccessBlockConfiguration'].values())
def download(ref,suffix='.tar.gz'):
 p=cache/(ref['sha256']+suffix);u=urlparse(ref['uri'])
 if not p.exists():s3.download_file(u.netloc,u.path.lstrip('/'),str(p),ExtraArgs={'ExpectedBucketOwner':owner})
 if sha(p)!=ref['sha256']:raise RuntimeError('Archived input checksum mismatch: '+ref['uri'])
 return p
download(spec['input_restore_index'],'.json')
plan=read(BUNDLE/'restore-plan.json')
for item in plan:
 name=Path(item['path'])
 if name.is_absolute() or '..' in name.parts or name.parts[0]!='data':raise RuntimeError('Unsafe restore path')
 target=ROOT/'application'/name
 with tarfile.open(download(item['archive'])) as tar:
  member=tar.getmember(str(name))
  if not member.isfile():raise RuntimeError('Unexpected non-file input')
  target.parent.mkdir(parents=True,exist_ok=True)
  with tar.extractfile(member) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
 if sha(target)!=item['sha256']:raise RuntimeError('Restored scientific input changed: '+str(name))
report={'status':'verified','owner':owner,'files':[{k:x[k] for k in ['path','sha256']} for x in plan]}
(BUNDLE/'input-verification.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'status':'verified','scientific_files':len(plan)}))
