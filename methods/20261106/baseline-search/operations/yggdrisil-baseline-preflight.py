import hashlib,json,os,subprocess,sys
from pathlib import Path
from importlib.metadata import version,distributions
from datetime import datetime,timezone
from urllib.request import Request,urlopen
import boto3,wandb
from scripts.run_deletion_baseline import load_bundle,validate_approval,verify_sources
from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.data.io import file_sha256
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.scorers.fba import FBAScorer
from yggdrisil_ecoli.scorers.rba import RBAScorer
r=Path('/home/ubuntu/yggdrisil-baselines-20261106');b=r/'bundle';app=b/'source/application'
spec,_=load_bundle(b,0);approval=json.loads((b/'approval.json').read_bytes());validate_approval(b,spec,approval);sources=verify_sources(b,app)
assert sys.platform=='linux' and not (r/'batch-progress.json').exists()
with urlopen(Request('http://169.254.169.254/latest/api/token',method='PUT',headers={'X-aws-ec2-metadata-token-ttl-seconds':'60'}),timeout=10) as f:token=f.read().decode()
with urlopen(Request('http://169.254.169.254/latest/meta-data/instance-id',headers={'X-aws-ec2-metadata-token':token}),timeout=10) as f:instance=f.read().decode()
assert instance==approval['instance_id']
inputs={k:app/v['path'] for k,v in spec['inputs'].items()}
for k,p in inputs.items():assert file_sha256(p)==spec['inputs'][k]['sha256'],k
versions={k:version(k) for k in spec['required_versions']};assert versions==spec['required_versions']
genes=load_genes(inputs['genes']);assert len(genes)==4290
scorers=[GenomeSizeScorer(genes),EssentialityScorer(genes=genes,artifact_hash=file_sha256(inputs['genes'])),ModuleEvaluator.from_json(inputs['modules'],genes),FBAScorer(genes=genes,model_path=inputs['fba_model']),RBAScorer(genes=genes,artifact_dir=inputs['rba_manifest'].parent)]
identities=active_evaluator_ids(scorers);assert identities==spec['evaluator_ids']
assert '18 passed' in (r/'offline-tests.log').read_text()
os.environ.update(json.loads((r/'credentials/tracking.json').read_text()));assert wandb.Api(timeout=60).viewer
s3=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts',region_name='us-east-1').get_caller_identity()['Account'];s3.head_bucket(Bucket=spec['tracking']['s3_bucket'],ExpectedBucketOwner=owner);assert all(s3.get_public_access_block(Bucket=spec['tracking']['s3_bucket'],ExpectedBucketOwner=owner)['PublicAccessBlockConfiguration'].values())
timer=subprocess.check_output(['systemctl','list-timers','yggdrisil-baseline-budget-stop.timer','--no-pager'],text=True);assert datetime.fromtimestamp(approval['stop_deadline_unix'],timezone.utc).strftime('%Y-%m-%d %H:%M:%S') in timer
record={'status':'passed','checked_at':datetime.now(timezone.utc).isoformat(),'instance_id':instance,'source_file_hashes':sources,'versions':versions,'evaluator_ids':identities,'registry_genes':len(genes),'model_calls':0,'tests':18,'wandb_authenticated':True,'s3_owner_verified':True,'absolute_timer':timer,'packages':{d.metadata['Name']:d.version for d in distributions()},'operational_sources':{p.name:file_sha256(p) for p in r.glob('yggdrisil-*.py')}}
(r/'preflight.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps({k:v for k,v in record.items() if k not in ['source_file_hashes','packages','operational_sources']}))
