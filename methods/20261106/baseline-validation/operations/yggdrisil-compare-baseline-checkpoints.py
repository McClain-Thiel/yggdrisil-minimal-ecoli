import concurrent.futures
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import boto3

OUT = Path('/private/tmp/yggdrisil-baseline-final-audit')
read = lambda p: json.loads(p.read_bytes())
expected = {f['path']:f for f in read(OUT/'final-index.json')['files']}
s3 = boto3.client('s3',region_name='us-east-1')
owner = boto3.client('sts').get_caller_identity()['Account']
result = {'jobs':{}, 'status':'running'}
for label in ['calibration','seed-101','seed-211','seed-307']:
    files = read(OUT/(label+'-index.json'))['files']
    prog = read(OUT/'metadata/runs'/label/'progress.json')
    output = Path(prog['experiment_dir']).name
    jobs, comparisons, checkpoint_only, metadata_changes = [], [], [], []
    for name, ref in files.items():
        mapped = 'runs/'+label+'/'+name
        if name.startswith('output/'):
            mapped = 'runs/'+label+'/output/'+output+'/'+name.removeprefix('output/')
        if name=='variant-knockout-audit.json':
            mapped='knockout-audit/variant-knockout-audit.json'
        if name=='inputs/genes.parquet':
            mapped=name
        if mapped in expected:
            exp=expected[mapped]
            if ref['sha256']!=exp['sha256']:
                assert name in {'experiments.json','events.jsonl'},(label,name)
                metadata_changes.append({'path':name,'checkpoint_sha256':ref['sha256'],'archive_sha256':exp['sha256']})
            else:
                assert ref['bytes']==exp['bytes']
                comparisons.append({'path':name,'archive_path':mapped,'sha256':exp['sha256'],'mode':'identical'})
        elif name.endswith('.gz') and mapped.removesuffix('.gz') in expected:
            jobs.append((name,ref,mapped.removesuffix('.gz')))
        else:
            assert name.startswith('inputs/graph-') and name.endswith('.sqlite'),(label,name)
            checkpoint_only.append({'path':name,**ref,'reason':'Restored search graph input retained in independently verified S3/W&B checkpoints; final data archive omits the restore staging directory.'})
    def check(item):
        name,ref,mapped=item
        u=urlparse(ref['uri'])
        with s3.get_object(Bucket=u.netloc,Key=u.path.lstrip('/'),ExpectedBucketOwner=owner)['Body'] as body:
            data=body.read()
        assert len(data)==ref['bytes'] and hashlib.sha256(data).hexdigest()==ref['sha256']
        raw=gzip.decompress(data);exp=expected[mapped]
        assert len(raw)==exp['bytes'] and hashlib.sha256(raw).hexdigest()==exp['sha256'],(label,name)
        return {'path':name,'archive_path':mapped,'sha256':exp['sha256'],'compressed_sha256':ref['sha256'],'mode':'decompressed_identical'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        comparisons.extend(pool.map(check,jobs))
    result['jobs'][label]={'comparisons':comparisons,'checkpoint_only_inputs':checkpoint_only,'mutable_catalog_differences':metadata_changes,'all_closed_scientific_outputs_match_final_archive':True}
    print(json.dumps({'job':label,'matched':len(comparisons),'checkpoint_only_inputs':len(checkpoint_only)}),flush=True)
result.update(status='verified',completed_at=datetime.now(timezone.utc).isoformat())
(OUT/'checkpoint-archive-comparison.json').write_text(json.dumps(result,indent=2)+'\n')
