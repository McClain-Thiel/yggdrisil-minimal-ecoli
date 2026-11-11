"""Freeze continuation finalists and prepare the approved validation on Linux."""
import copy,gzip,hashlib,json,os,shutil,sqlite3,subprocess,sys,tarfile
from contextlib import closing
from pathlib import Path
from datetime import datetime,timezone
import boto3
from yggdrisil_ecoli.vecoli import select_finalists,map_finalists,install_vecoli_adapter
from yggdrisil_ecoli.data.evidence import load_genes

ROOT=Path('/home/ubuntu/yggdrisil-continuation-20261106')
PLAN=json.loads((ROOT/'protocol.json').read_text());BUNDLE=ROOT/'application/runs'/PLAN['experiment_id']
VALID=ROOT/'validation';VALID.mkdir(exist_ok=True)
if (VALID/'prepared.json').exists():raise RuntimeError('Validation already prepared; inspect before retry')
progress=json.loads((ROOT/'search-ended.json').read_text())
if progress['status']!='completed' or not progress['all_model_children_stopped']:raise RuntimeError('Three searches must finish successfully before fixed finalist validation')
def sha(path):
 with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def snapshot_graph(source, destination):
 # Connection context managers commit transactions but do not close handles.
 with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
  with closing(sqlite3.connect(destination)) as dst:
   src.backup(dst)
   if dst.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise RuntimeError('Graph backup failed integrity check')
   result=dst.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
   if result[0]!=0:raise RuntimeError('Graph backup checkpoint was busy')
   if dst.execute('PRAGMA journal_mode=DELETE').fetchone()!=('delete',):raise RuntimeError('Graph backup did not leave WAL mode')
 for suffix in ['-wal','-shm']:
  if Path(str(destination)+suffix).exists():raise RuntimeError('Closed graph backup still has sidecars')
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
archive=ROOT/'input-archives/9e01ed0650d2de9f9f9f7b80856c71d34d27b013e1f4046aff450e80b8dfdb4b.tar.gz'
if not archive.exists():boto3.client('s3',region_name='us-east-1').download_file('phd-research-storage-1758274488','yggdrisil/archives/20261105/vecoli-kimi-full-diagnostic-20261105-inputs-v0/'+archive.name,str(archive))
assert sha(archive)==archive.name.split('.')[0]
inputs=VALID/'inputs';inputs.mkdir(exist_ok=True)
with tarfile.open(archive) as tar:
 for name in ['manifest.json','workflow.json','inputs/simData.cPickle.gz']:
  target=inputs/Path(name).name
  with tar.extractfile(name) as src,target.open('wb') as dst:shutil.copyfileobj(src,dst)
base=json.loads((inputs/'manifest.json').read_text());workflow=json.loads((inputs/'workflow.json').read_text())
sim=inputs/'simData.cPickle'
with gzip.open(inputs/'simData.cPickle.gz','rb') as src,sim.open('wb') as dst:shutil.copyfileobj(src,dst)
assert sha(sim)==base['workflow']['sim_data_sha256']
genes_path=ROOT/'application/data/processed/genes.parquet';genes=load_genes(genes_path)
finalists=copy.deepcopy(base['finalists'][:2]);sources=[]
for index,arm in enumerate(PLAN['arms']):
 folder=BUNDLE/'search'/arm['execution_id'];report=json.loads((folder/'result.json').read_text());assert report['status']=='completed'
 graph=inputs/f'{arm["arm"]}.sqlite'
 snapshot_graph(folder/'graph.sqlite',graph)
 selection,frame=select_finalists(graph,count=1,deletion_band=1.0)
 assert frame.index[0]==report['finalist']['state_id'],'Frozen candidate differs from declared finalist'
 item=map_finalists(frame,genes).reset_index().to_dict('records')[0]
 item.update(name=arm['arm'].replace('_','-'),variant_index=index+2)
 finalists.append(item)
 sources.append({'selection':selection,'backup':{'frozen_path':str(graph),'frozen_sha256':sha(graph)},'parent_search_execution_id':arm['execution_id']})
# The original checkout remains clean; the patched checkout is separately pinned.
native=ROOT/'native-validated'
if not native.exists():
 subprocess.run(['git','clone','--shared',str(ROOT/'native'),str(native)],check=True)
 (native/'.venv').symlink_to(ROOT/'native/.venv',target_is_directory=True)
 subprocess.run(['git','apply',str(ROOT/'solver-recovery-v2.patch')],cwd=native,check=True)
assert sha(native/'wholecell/utils/_netflow/nf_glpk.py')=='5ad8cd9ca727381032bb852b2b786465424598fbba1c261ac36741377db01686'
adapter=install_vecoli_adapter(native)
# Audit the exact proposed genotypes with the pinned native dependency environment.
audit_manifest=copy.deepcopy(base);audit_manifest['finalists']=finalists;audit_manifest['workflow']['sim_data_path']=str(sim)
save(VALID/'manifest.json',audit_manifest);save(VALID/'preflight.json',{'status':'preparing'})
env=os.environ.copy();env['PYTHONPATH']=str(native)
for k in ['WANDB_API_KEY','OPENROUTER_API_KEY','EDISON_PLATFORM_API_KEY']:env.pop(k,None)
subprocess.run([str(native/'.venv/bin/python'),str(VALID/'audit-knockouts.py')],env=env,cwd=native,check=True)
audit_manifest=json.loads((VALID/'manifest.json').read_text());finalists=audit_manifest['finalists']
controller=ROOT/'validation-application/scripts/track_vecoli_experiment.py'
for seed in [None,*PLAN['validation']['seeds']]:
 calibration=seed is None;seed=101 if calibration else seed;label='calibration' if calibration else f'seed-{seed}'
 folder=VALID/'runs'/label;folder.mkdir(parents=True,exist_ok=False)
 for name in ['runscripts','ecoli']:(folder/name).symlink_to(native/name,target_is_directory=True)
 exp=PLAN['validation']['experiment_id']+'-'+label
 config=copy.deepcopy(workflow);config.update(experiment_id=exp.replace('-','_'),generations=2 if calibration else 20,lineage_seed=seed,SIM_CPUS=5,SIM_MEM=4,sim_data_path=str(sim))
 config['emitter_arg']['out_dir']=str(folder/'output')
 config['variants']={} if calibration else {'yggdrisil_multi_gene_knockout':{'gene_ids':{'value':[x['vecoli_gene_ids'] for x in finalists[1:]]}}}
 config['skip_baseline']=False
 save(folder/'workflow.json',config)
 manifest=copy.deepcopy(base);manifest.update(experiment_id=exp,created_at=datetime.now(timezone.utc).isoformat(),purpose='Fixed continuation finalists with uniformly applied strict numerical recovery',source_graphs=sources,finalists=finalists[:1] if calibration else finalists)
 manifest['parent_experiment_ids']=[PLAN['experiment_id'],PLAN['parent_validation_id']]
 manifest['parent_data_artifact']=progress['latest_checkpoint']['wandb_artifact']
 manifest['lineage'].update(seed=seed,max_generations=config['generations']);manifest['validation'].update(require_generations=config['generations'])
 manifest['controller_source']={'path':str(controller),'sha256':sha(controller)}
 manifest['execution']={'backend':'Nextflow local executor on dedicated remote Linux EC2','vecoli_python':str(native/'.venv/bin/python'),'nextflow_executable':str(ROOT/'bin/nextflow'),'workflow_working_directory':str(folder),'max_parallel_cells':1 if calibration else 3,'paid_compute':True}
 manifest['registry']={'path':str(genes_path),'sha256':sha(genes_path)}
 manifest['vecoli'].update(checkout=str(native),adapter_path=str(adapter),adapter_sha256=sha(adapter),variant_knockout_audit_path=str(VALID/'variant-knockout-audit.json'),variant_knockout_audit_sha256=sha(VALID/'variant-knockout-audit.json'),solver_patch_sha256=PLAN['validation']['solver_patch_sha256'],solver_after_sha256=sha(native/'wholecell/utils/_netflow/nf_glpk.py'))
 manifest['workflow'].update(config_path=str(folder/'workflow.json'),config_sha256=sha(folder/'workflow.json'),experiment_id=config['experiment_id'],output_root=config['emitter_arg']['out_dir'],sim_data_path=str(sim),sim_data_sha256=sha(sim))
 manifest['tracking'].update(run_id=hashlib.sha256(exp.encode()).hexdigest()[:12],run_name=exp,artifact_name=exp+'-data',s3_prefix=PLAN['tracking']['validation_s3_prefix']+label,tags=['continuation','lineage20','strict-solver-recovery',label])
 manifest['solver_policy']=PLAN['validation']['solver_policy']
 save(folder/'manifest.json',manifest);save(folder/'experiments.json',{'experiments':[{'id':exp,'status':'prepared','data_backup':{}}]})
save(VALID/'prepared.json',{'prepared_at':datetime.now(timezone.utc).isoformat(),'genotypes':[{k:x[k] for k in ['name','state_id','deletion_count','deletion_set_sha256']} for x in finalists],'source_graphs':sources,'solver_patch_sha256':PLAN['validation']['solver_patch_sha256']})
print('Prepared 5 frozen genotypes, 3 seeds, 20 generations; exact knockout audit passed')
