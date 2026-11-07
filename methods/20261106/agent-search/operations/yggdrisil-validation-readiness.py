import hashlib,json,subprocess,tarfile
from pathlib import Path
from datetime import datetime,timezone
import boto3
from yggdrisil_ecoli.vecoli import validate_vecoli_checkout
r=Path('/home/ubuntu/yggdrisil-continuation-20261106')
assert '28 passed' in (r/'validation-tests-final.log').read_text()
assert '\nOK\n' in (r/'validation-solver-tests.log').read_text()
original=validate_vecoli_checkout(r/'native');patched=validate_vecoli_checkout(r/'native-validated')
assert original['git_commit']==patched['git_commit'] and original['uv_lock_sha256']==patched['uv_lock_sha256']
sha='9e01ed0650d2de9f9f9f7b80856c71d34d27b013e1f4046aff450e80b8dfdb4b'
archive=r/'input-archives'/f'{sha}.tar.gz'
if not archive.exists():boto3.client('s3',region_name='us-east-1').download_file('phd-research-storage-1758274488',f'yggdrisil/archives/20261105/vecoli-kimi-full-diagnostic-20261105-inputs-v0/{sha}.tar.gz',str(archive))
with archive.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==sha
with tarfile.open(archive) as tar:
 manifest=json.load(tar.extractfile('manifest.json'));workflow=json.load(tar.extractfile('workflow.json'))
assert [g['deletion_count'] for g in manifest['finalists'][:2]]==[0,1030]
assert workflow['single_daughters'] and workflow['n_init_sims']==1 and workflow['fail_at_max_duration']
print(json.dumps({'workflow_options':workflow,'lineage':manifest['lineage'],'validation':manifest['validation']}))
files=[r/'yggdrisil-prepare-continuation-validation.py',r/'yggdrisil-continuation-validation-supervisor.py',r/'validation-application/scripts/track_vecoli_experiment.py',r/'validation-application/src/yggdrisil_ecoli/vecoli.py',r/'validation-application/src/yggdrisil_ecoli/vecoli_results.py',r/'validation/audit-knockouts.py',r/'solver-recovery-v2.patch',r/'validation-tests-final.log',r/'validation-solver-tests.log']
record={'status':'passed','checked_at':datetime.now(timezone.utc).isoformat(),'offline_validation_tests':28,'solver_tests_log':'validation-solver-tests.log','original_native':original,'patched_native':patched,'input_archive_sha256':sha,'files':{str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},'exact_knockout_audit':'Required after fixed final candidate selection, before calibration or full cells','runtime':'remote Linux only','science_deadline':json.loads((r/'deadlines.json').read_text())['science_deadline']}
(r/'validation-readiness.json').write_text(json.dumps(record,indent=2)+'\n')
