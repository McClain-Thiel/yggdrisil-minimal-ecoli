import fcntl
import hashlib
import importlib.metadata
import json
import netrc
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
OP = REPO/'runs/vecoli-deletion-baselines-20gen-3seed-20261106'
OUT = Path('/private/tmp/yggdrisil-baseline-final-audit')
FINAL = OP/'final'
FINAL.mkdir(exist_ok=True)
sys.path.insert(0,str(REPO/'src'))
from yggdrisil_ecoli.baseline_storage import BaselineArchive, configured_secrets, screen
from yggdrisil_ecoli.data.io import atomic_json, file_sha256
import boto3

read = lambda p: json.loads(p.read_bytes())
now = lambda: datetime.now(timezone.utc).isoformat()
spec, auth = read(OP/'protocol.json'), read(OP/'approval.json')
assert auth['total_cap_usd'] == 30
assert file_sha256(OP/'protocol.json') == auth['protocol_sha256']
secrets = configured_secrets(REPO,REPO/'runs/literature-ablation-kimi-continuation-20261106/credentials')
archive = BaselineArchive(s3=boto3.client('s3',region_name='us-east-1'),wandb=None,tracked=None,
    owner=boto3.client('sts').get_caller_identity()['Account'], bucket=spec['tracking']['s3_bucket'],
    prefix=spec['tracking']['s3_prefix'],run_id='study',secrets=secrets)

if sys.argv[1] == 'preserve':
    audit = read(OUT/'archive-member-audit.json')
    artifacts = read(OUT/'artifact-verification.json')
    comparison = read(OUT/'checkpoint-archive-comparison.json')
    assert audit['status'] == artifacts['status'] == comparison['status'] == 'verified'
    assert len(artifacts['jobs']) == 4
    os.environ['WANDB_BASE_URL'] = 'https://api.forge.coreweave.com'
    os.environ['WANDB_API_KEY'] = netrc.netrc().authenticators('api.wandb.ai')[2]
    os.environ['WANDB_CACHE_DIR'] = str(OUT/'wandb-cache')
    import wandb
    api = wandb.Api(timeout=120)
    original = api.artifact(artifacts['final_artifact']['name'])
    assert original.state == 'COMMITTED' and original.digest == artifacts['final_artifact']['digest']
    immutable = original.qualified_name.rsplit(':',1)[0]+':'+original.version
    exact = api.artifact(immutable)
    assert exact.id == original.id and exact.digest == original.digest
    artifacts['final_artifact'].update(name=immutable,version=original.version,id=original.id)
    atomic_json(OUT/'artifact-verification.json',artifacts)
    frozen = {}
    for name in ['protocol.json','genotypes.json','manifest.json','source-manifest.json','deployment-manifest.json','approval.json']:
        assert file_sha256(OP/name) == file_sha256(OUT/'metadata'/name), name
        frozen[name] = file_sha256(OP/name)
    index_ref = {'uri':spec['tracking']['s3_prefix'], 'sha256':file_sha256(OUT/'final-index.json'),'bytes':(OUT/'final-index.json').stat().st_size}
    index_ref['uri'] = f"s3://{spec['tracking']['s3_bucket']}/{spec['tracking']['s3_prefix']}study/final/{index_ref['sha256']}.json"
    verification = {'experiment_id':spec['experiment_id'],'status':'independently_verified','verified_at':now(),
        'archive':audit['archive'],'index':index_ref,'members':audit['members'],'member_bytes':audit['member_bytes'],
        'full_s3_get_and_all_member_names_sizes_sha256_verified':True,
        'final_archive_and_index_wandb_download_verified':True,'final_wandb_artifact':artifacts['final_artifact'],
        'final_checkpoint_artifacts':artifacts['jobs'],'all_closed_checkpoint_outputs_match_final_archive':True,
        'input_graphs': 'Ten restored SQLite search inputs are preserved in each S3/W&B checkpoint; they are not in the final tar because restore staging is excluded.',
        'frozen_input_and_provenance_hashes':frozen,
        'source_integrity_scope':'Deployed operational and native source hashes were checked live at the last running monitor; frozen manifests match the final archive. No stopped-disk source hash claim is made.',
        'original_failed_setup_attempt_preserved':read(OUT/'metadata/setup-attempt-backup.json'),
        'scientific_execution':'remote Linux only; no scientific reruns during independent archive verification',
        'exclusions':'Source code, credentials, environment and W&B caches remain excluded from data uploads.'}
    verification['analysis_source_sha256']={name:file_sha256(Path('/private/tmp')/name) for name in ['yggdrisil-audit-baseline-final.py','yggdrisil-verify-baseline-artifacts.py','yggdrisil-compare-baseline-checkpoints.py','yggdrisil-analyze-baseline-final.py','yggdrisil-publish-baseline-final.py','yggdrisil-clean-baseline-worker.py']}
    verification['archival_analysis_environment']={'python':sys.version,'pyarrow':importlib.metadata.version('pyarrow'),'wandb':wandb.__version__,'boto3':boto3.__version__,'execution_scope':'Local read-only archival verification and compact analysis; all simulations ran on remote Linux.'}
    atomic_json(OUT/'final-backup-verification.json',verification)
    filenames = ['final-analysis.json','final-backup-verification.json','final-index.json','archive-member-audit.json',
        'artifact-verification.json','artifact-verification.jsonl','checkpoint-archive-comparison.json','terminal-evidence.json','worker-before-cleanup.json']
    refs = {}
    for name in filenames:
        source = OUT/name
        screen(source,secrets)
        shutil.copyfile(source,FINAL/name)
        refs[name] = archive.upload(source,'final-analysis')
    for source in sorted((OUT/'metadata').rglob('*.json')):
        screen(source,secrets)
        name = str(source.relative_to(OUT/'metadata'))
        target = FINAL/'remote'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
    with tempfile.TemporaryDirectory(prefix='analysis-tracking-',dir=OUT) as scratch:
        with wandb.init(entity='mcclain',project='yggdrisil',id='vecoli-baselines-final-audit-20261107',resume='allow',
            name=spec['experiment_id']+'-final-analysis',job_type='analysis',tags=['yggdrisil','baseline','validation','final-audit'],
            config={'experiment_id':spec['experiment_id'],'protocol_sha256':auth['protocol_sha256'],'scientific_rerun':False},
            dir=scratch,settings=wandb.Settings(disable_git=True,disable_code=True,save_code=False,x_save_requirements=False)) as tracked:
            result = read(OUT/'final-analysis.json')
            tracked.summary.update(result['summary'])
            art = wandb.Artifact(spec['experiment_id']+'-final-analysis',type='analysis',metadata={'s3':refs})
            for name in ['final-analysis.json','final-backup-verification.json','final-index.json','checkpoint-archive-comparison.json','terminal-evidence.json']:
                art.add_file(str(FINAL/name),name=name)
            saved=tracked.log_artifact(art);saved.wait()
            remote=api.artifact(saved.qualified_name)
            assert remote.state=='COMMITTED'
            for name in remote.manifest.entries:
                path=Path(remote.get_entry(name).download(root=scratch+'/verify',skip_cache=True))
                assert file_sha256(path)==refs[name]['sha256'] and path.stat().st_size==refs[name]['bytes']
            analysis_artifact={'name':saved.qualified_name,'version':remote.version,'id':remote.id,'digest':remote.digest,'state':remote.state,'run_url':tracked.url,'all_files_download_sha256_verified':True}
    publication={'experiment_id':spec['experiment_id'],'status':'verified_cleanup_pending','published_at':now(),'records':refs,'analysis_artifact':analysis_artifact,'full_configured_secret_scan':True,'full_s3_round_trip_verified':True}
    atomic_json(FINAL/'publication.json',publication)
    screen(FINAL/'publication.json',secrets)
    receipt=archive.upload(FINAL/'publication.json','final-analysis')
    atomic_json(FINAL/'publication-receipt.json',receipt)
    print(json.dumps({'status':publication['status'],'receipt':receipt,'artifact':analysis_artifact}),flush=True)
elif sys.argv[1] == 'ledger':
    publication=read(FINAL/'publication.json');verification=read(FINAL/'final-backup-verification.json')
    result=read(FINAL/'final-analysis.json');cleanup=read(OUT/'worker-cleanup.json')
    assert cleanup['status']=='terminated_and_volume_deleted' and cleanup['shared_worker_state']=='running'
    shutil.copyfile(OUT/'worker-cleanup.json',FINAL/'worker-cleanup.json')
    screen(FINAL/'worker-cleanup.json',secrets);cleanup_ref=archive.upload(FINAL/'worker-cleanup.json','final-cleanup')
    with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        canonical=REPO/'experiments.json';working=Path('/private/tmp/yggdrisil-migration-ledger.json')
        before=canonical.read_bytes();ledger=json.loads(before);assert ledger==read(working)
        assert ledger['storage_migration']['status']=='completed_verified'
        entry=next(e for e in ledger['experiments'] if e['id']==spec['experiment_id'])
        (OP/'attempts'/('ledger-before-final-'+hashlib.sha256(before).hexdigest()+'.json')).write_bytes(before)
        batch=read(FINAL/'remote/batch-progress.json')
        entry.update(status='completed_verified',scientific_status=result['status'],last_verified_at=now(),finished_at=batch['updated_at'],
            current_progress={'status':'completed_verified','summary':result['summary'],'original_workflow_statuses':{k:v['status'] for k,v in batch['jobs'].items()},'no_active_scientific_processes':True},
            final_results=result['summary'],final_analysis=publication['records']['final-analysis.json'],final_verification=publication['records']['final-backup-verification.json'],
            cleanup=cleanup_ref,worker_cleanup=cleanup,remote_execution_only=True,local_final_metadata=str(FINAL),
            interpretation=result['interpretation'],comparison_limits=result['comparison_limits'])
        entry['worker'].update(status='terminated',volume_status='deleted',cleanup_verified_at=cleanup['verified_at'])
        entry['data_backup'].update(status='verified_complete',final_archive=verification['archive'],final_index=verification['index'],
            final_wandb_artifact=verification['final_wandb_artifact'],final_checkpoint_artifacts=verification['final_checkpoint_artifacts'],
            final_analysis_records=publication['records'],final_analysis_wandb_artifact=publication['analysis_artifact'],
            all_archive_members_independently_verified=True,full_s3_get_verified=True,all_final_wandb_downloads_verified=True)
        urls=[batch['wandb_url'],publication['analysis_artifact']['run_url']]
        for label in ['calibration','seed-101','seed-211','seed-307']:
            urls += read(FINAL/'remote/runs'/label/'experiments.json')['experiments'][0]['wandb_run_urls']
        entry['wandb_run_urls']=sorted(set(urls))
        parent=next(e for e in ledger['experiments'] if e['id']=='deletion-baselines-20261106')
        assert parent['status']=='completed_verified'
        parent['held_out_validation'].update(status='completed_verified',scientific_status=result['status'],result_summary=result['summary'],analysis=entry['final_analysis'])
        ledger['updated_at']=now();staged=FINAL/'ledger-to-publish.json';atomic_json(staged,ledger);screen(staged,secrets)
        ledger_ref=archive.upload(staged,'ledger')
        assert canonical.read_bytes()==before and read(working)==json.loads(before)
        atomic_json(canonical,ledger);atomic_json(working,ledger)
        assert read(canonical)==read(working)
        completion={'status':'completed_verified','completed_at':now(),'analysis':entry['final_analysis'],'verification':entry['final_verification'],'cleanup':cleanup_ref,'ledger':ledger_ref,'summary':result['summary']}
        atomic_json(FINAL/'completion.json',completion);screen(FINAL/'completion.json',secrets)
        receipt=archive.upload(FINAL/'completion.json','final-cleanup')
        atomic_json(FINAL/'completion-receipt.json',receipt)
        previous=OP/'publication.json'
        if previous.exists():shutil.copyfile(previous,OP/'attempts'/('publication-before-final-'+file_sha256(previous)+'.json'))
        atomic_json(previous,{**publication,**completion,'completion_receipt':receipt})
        for source in sorted((FINAL/'remote').rglob('*.json')):
            name=source.relative_to(FINAL/'remote')
            if str(name)=='monitoring-summary.json':
                continue
            target=OP/'remote'/name
            if target.exists() and file_sha256(target)!=file_sha256(source):
                prior=OP/'attempts'/'metadata-before-final'/file_sha256(target)/name
                prior.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(target,prior)
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
        current=OP/'remote/monitoring-summary.json'
        if current.exists():shutil.copyfile(current,OP/'attempts'/('monitoring-before-final-'+file_sha256(current)+'.json'))
        atomic_json(current,{'status':'completed_verified','verified_at':now(),'batch':batch,'summary':result['summary'],
            'archive_verification':entry['final_verification'],'worker_cleanup':cleanup_ref,'final_analysis':entry['final_analysis']})
        worker=read(OP/'worker-launch.json');worker.update(status='terminated',volume_status='deleted',cleanup_verified_at=cleanup['verified_at'])
        atomic_json(OP/'worker-launch.json',worker)
    for name in ['yggdrisil-audit-baseline-final.py','yggdrisil-verify-baseline-artifacts.py','yggdrisil-compare-baseline-checkpoints.py','yggdrisil-analyze-baseline-final.py','yggdrisil-publish-baseline-final.py','yggdrisil-clean-baseline-worker.py']:
        shutil.copyfile(Path('/private/tmp')/name,OP/'source'/name)
    print(json.dumps(completion),flush=True)
else:
    raise RuntimeError('Expected preserve or ledger stage')
