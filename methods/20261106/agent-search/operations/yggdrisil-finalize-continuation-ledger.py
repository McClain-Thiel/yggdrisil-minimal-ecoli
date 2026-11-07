import fcntl
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path

root=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
op=root/'runs/literature-ablation-kimi-continuation-20261106'
read=lambda p:json.loads(p.read_bytes())
a=read(op/'final-analysis.json');audit=read(op/'final-backup-verification.json');pub=read(op/'completion-record-publication.json');w=read(op/'final-analysis-artifact.json');cleanup=read(op/'resource-cleanup.json');monitor=read(op/'monitoring-final-progress.json');cost=read(op/'provider-accounting.json')
assert audit['status']=='verified' and w['all_files_downloaded_sha256_verified']
assert cleanup['instance_state']=='terminated' and cleanup['volume_deleted']
assert cost['all_disabled'] and a['original_completed_cycles']==294
with Path('/private/tmp/yggdrisil-migration.lock').open('a+') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 p=root/'experiments.json';old=p.read_bytes();d=json.loads(old)
 working=Path('/private/tmp/yggdrisil-migration-ledger.json');assert read(working)==d
 (op/('ledger-before-final-'+hashlib.sha256(old).hexdigest()+'.json')).write_bytes(old)
 e=next(x for x in d['experiments'] if x['id']==a['experiment_id']);s=next(x for x in d['experiments'] if x['id']=='literature-ablation-kimi-continuation-20261106')
 e.update(status='completed_with_exceptions_verified',completed_at='2026-11-06T14:50:57Z',last_verified_at=audit['completed_at'],completed_full_cell_cycles=294,lineages_at_20=14,public_ip=None,final_worker_state='terminated',resource_cleanup=cleanup,readiness='All completed outputs independently audited, final S3 and W&B copies verified, dedicated worker and volume removed.')
 e['workflow_checkpoints']={name:{'status':j['catalog']['status'],'data_backup':j['catalog']['data_backup'],'artifact_verified':True,'verified_progress':j['verified_progress']} for name,j in monitor['validation_jobs'].items()}
 e['final_analysis']={'report':pub['records']['final-analysis.json'],'wandb':w,'interpretation':a['interpretation'],'lineages':[{k:x[k] for k in ['name','state_id','seed','deletion_count','deletion_set_sha256','generations_completed','terminal_reason','late_last5','late_last4','birth_dry_mass_last_over_first','solver_assisted_generations']} for x in a['original_lineages']],'numerical_warning_counts':a['warning_counts'],'solver_assisted_cells':18,'finite_mass_time_checks':'passed','daughter_target_transcript_checks':'passed'}
 e['data_backup']={'status':'verified','final_archive':audit['final_archive'],'final_index':audit['final_index'],'member_audit':audit['archive_verification'],'independent_verification':pub['records']['final-backup-verification.json'],'final_wandb_artifacts':audit['wandb_artifacts'],'completion_records':pub['records']}
 e['solver_recovery_verification']={'report':pub['records']['solver-monitoring.json'],'accepted_fallback_count':18,'protocol':'Uniform predefined solver patch; unchanged model and tolerances; each accepted fallback passes exact LP and strict KKT checks.'}
 e['launcher']=monitor['validation_launcher'];e['last_monitor_record']=pub['records']['monitoring-final-progress.json'];e['operational_final_record']=pub['records']['operational-final-record.json']
 if w['run_url'] not in e['wandb_run_urls']:e['wandb_run_urls'].append(w['run_url'])
 s.update(status='search_and_validation_complete_with_exceptions',last_verified_at=audit['completed_at'],final_worker_state='terminated',public_ip=None,resource_cleanup=cleanup,model_spend=cost,validation_result={'id':e['id'],'status':e['status'],'report':pub['records']['final-analysis.json'],'lineages_at_20':14,'lineages_total':15,'known_model_exception':'no-retrieval seed307 generation15 after14 divisions'})
 s['notes']='All three continuation searches completed and keys disabled. Finalists and every original attempt retained. Subsequent validation completed with one model exception; no laboratory viability or global minimality claim.'
 assert d['storage_migration']['status']=='completed_verified'
 assert next(x for x in d['experiments'] if x['id']=='deletion-baselines-20261106')['status']=='prepared_not_launched'
 assert p.read_bytes()==old and read(working)==json.loads(old)
 d['updated_at']=datetime.now(timezone.utc).isoformat();data=(json.dumps(d,indent=2)+'\n').encode()
 for path in [p,working]:
  temp=path.with_suffix('.final.tmp');temp.write_bytes(data);temp.replace(path)
print('Both ledgers record final analysis, exact verified references and cleanup; prepared baseline and prior studies preserved.')
