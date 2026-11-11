import hashlib,json,os,shutil
from pathlib import Path
from importlib.metadata import version
from decimal import Decimal
from datetime import datetime,timezone
import boto3
import wandb
import yggdrisil,yggdrisil_ecoli
from scripts.run_literature_ablation import load_job,source_hash,key_usage,check_key_budget
from scripts.plan_literature_ablation import checked_file
from yggdrisil_ecoli.data.evidence import load_genes
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.scorers.size import GenomeSizeScorer
from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
from yggdrisil_ecoli.scorers.fba import FBAScorer
from yggdrisil_ecoli.scorers.rba import RBAScorer
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
ROOT=Path('/home/ubuntu/yggdrisil-continuation-20261106')
shutil.copyfile(ROOT/'literature_ablation_continuation.json',ROOT/'protocol.json')
plan=json.loads((ROOT/'protocol.json').read_text());BUNDLE=ROOT/'application/runs'/plan['experiment_id']
shutil.copyfile(ROOT/'protocol.json',BUNDLE/'execution-plan.json')
m=json.loads((BUNDLE/'manifest.json').read_text());m['bundle_sha256']['execution-plan.json']=hashlib.sha256((BUNDLE/'execution-plan.json').read_bytes()).hexdigest();(BUNDLE/'manifest.json').write_text(json.dumps(m,indent=2)+'\n')
spec,_=load_job(BUNDLE,0)
inputs={n:checked_file(ROOT/'application',r) for n,r in spec['inputs'].items()}
genes=load_genes(inputs['genes']); assert len(genes)==4290
scorers=[GenomeSizeScorer(genes),EssentialityScorer(genes=genes,artifact_hash=spec['inputs']['genes']['sha256']),ModuleEvaluator.from_json(inputs['modules'],genes),FBAScorer(genes=genes,model_path=inputs['fba_model']),RBAScorer(genes=genes,artifact_dir=inputs['rba_manifest'].parent)]
ids=active_evaluator_ids(scorers)
assert ids==plan['continuation']['evaluator_ids'], 'Evaluator identities changed'
sources={'application':source_hash(yggdrisil_ecoli),'framework':source_hash(yggdrisil)}
assert sources==plan['continuation']['required_source_hashes'],'Scientific source mismatch'
versions={k:version(k) for k in plan['continuation']['required_versions']}
assert versions==plan['continuation']['required_versions'],'Dependency mismatch'
rows=[]
for i in range(3):
 _,job=load_job(BUNDLE,i)
 os.environ.update(json.loads((ROOT/f'credentials/arm-{i}.json').read_text()))
 usage=key_usage();assert check_key_budget(usage,Decimal('12'))==0
 rows.append({'arm':job['arm'],'provider_limit':usage['limit'],'usage':usage['usage'],'frozen_members':len(job['continuation']['member_sha256'])})
os.environ.pop('OPENROUTER_API_KEY')
os.environ.update(json.loads((ROOT/'credentials/common.json').read_text()))
assert wandb.Api().viewer,'W&B authentication failed'
client=boto3.client('s3',region_name='us-east-1');owner=boto3.client('sts',region_name='us-east-1').get_caller_identity()['Account'];client.head_bucket(Bucket='phd-research-storage-1758274488',ExpectedBucketOwner=owner)
files={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'application/scripts/run_literature_ablation.py',ROOT/'yggdrisil-continuation-controller.py',ROOT/'protocol.json',ROOT/'yggdrisil-restore-continuation.py']}
record={'checked_at':datetime.now(timezone.utc).isoformat(),'status':'passed','model_calls':0,'rows':rows,'sources':sources,'versions':versions,'evaluator_ids':ids,'bucket_owner_verified':True,'wandb_authenticated':True,'offline_tests_log':'search-tests-approved.log'}
(ROOT/'preflight.json').write_text(json.dumps(record,indent=2)+'\n');(ROOT/'source-snapshot.json').write_text(json.dumps({'files':files,'scientific_packages':sources,'versions':versions},indent=2)+'\n');print(json.dumps(record))
