import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import boto3
import wandb

root = Path('/home/ubuntu/yggdrisil-continuation-20261106')
publication = json.loads((root/'completion-record-publication.json').read_bytes())
os.environ.update(json.loads((root/'credentials/common.json').read_bytes()))
secrets = [v.encode() for p in (root/'credentials').glob('*.json') for k,v in json.loads(p.read_bytes()).items() if any(t in k.upper() for t in ('KEY','TOKEN','SECRET','PASSWORD')) and isinstance(v,str) and v]
s3 = boto3.client('s3', region_name='us-east-1')
assert publication['full_configured_secret_scan'] and publication['full_round_trip_verified']
sha = lambda b: hashlib.sha256(b).hexdigest()
with tempfile.TemporaryDirectory(prefix='final-analysis-', dir=root) as temporary:
    directory = Path(temporary)
    hashes = {}
    for name, ref in publication['records'].items():
        assert '/' not in name and name.endswith('.json')
        u = urlparse(ref['uri'])
        with s3.get_object(Bucket=u.netloc, Key=u.path.lstrip('/'))['Body'] as body:
            data = body.read()
        assert sha(data)==ref['sha256'] and len(data)==ref['bytes']
        assert not any(s in data for s in secrets)
        (directory/name).write_bytes(data)
        hashes[name]=ref['sha256']
    run = wandb.init(entity='mcclain',project='yggdrisil',id='continuationfinal20261106',name=publication['experiment_id']+'-final-analysis',job_type='analysis',tags=['yggdrisil','validation','final-analysis'],config={'experiment_id':publication['experiment_id'],'completed_divisions':294,'lineages_at_20':14,'known_model_exceptions':1,'solver_assisted_cells':18},dir=str(root))
    artifact = wandb.Artifact(publication['experiment_id']+'-final-analysis',type='analysis')
    for name in hashes:
        artifact.add_file(str(directory/name),name=name)
    saved = run.log_artifact(artifact);saved.wait();version=saved.qualified_name;run_url=run.url;run.finish()
    remote = wandb.Api(timeout=120).artifact(version)
    assert remote.state=='COMMITTED' and set(remote.manifest.entries)==set(hashes)
    with tempfile.TemporaryDirectory(prefix='final-artifact-verify-',dir=root) as check:
        downloaded=Path(remote.download(root=check))
        for name,digest in hashes.items():
            assert sha((downloaded/name).read_bytes())==digest,name
result={'experiment_id':publication['experiment_id'],'verified_at':datetime.now(timezone.utc).isoformat(),'artifact':version,'run_url':run_url,'state':'COMMITTED','all_files_downloaded_sha256_verified':True,'files':hashes,'s3_records':publication['records']}
(root/'final-analysis-artifact.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
