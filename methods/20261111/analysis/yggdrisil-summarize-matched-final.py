"""Summarize archived metadata and draw the explicitly exploratory comparison."""
import csv, hashlib, json, math, statistics
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
repo=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');op=repo/'runs/vecoli-depth-matched-20261111';old=repo/'runs/search-replication-20261109/parallel-validation/finalization'
read=lambda p:json.loads(p.read_bytes())
current=read(op/'final-checkpoint-metadata.json');nom=read(op/'matched-nominations.json');amend=read(op/'selection-amendment.json');audit=read(op/'final-checkpoint-independent-verification.json')
assert audit['status']=='verified_all_final_checkpoint_entries'
seeds=[101,211,307];current_lines={int(k.split('-')[1]):{x['state_id']:x for x in v['files']['progress.json']['value']['finalists']} for k,v in current['workflows'].items()}
old_lines={seed:{x['state_id']:x for x in read(old/f'metadata-{seed}/progress.json')['finalists']} for seed in seeds}
rows=[]
for candidate in amend['selection']['cohort']:
 rows.append({'method':candidate['method'],'search_seed':candidate['seed'],'source_run_id':candidate['run_id'],'state_id':candidate['state_id'],'deletions':candidate['genes_deleted'],'study':'search-replication-20261109','outcomes':[old_lines[seed][candidate['state_id']] for seed in seeds]})
for candidate in nom['runs']:
 f=candidate['finalist'];rows.append({'method':candidate['method'],'search_seed':candidate['seed'],'source_run_id':candidate['run_id'],'state_id':f['state_id'],'deletions':f['genes_deleted'],'study':op.name,'outcomes':[current_lines[seed][f['state_id']] for seed in seeds]})
assert len(rows)==25 and all(len(x['outcomes'])==3 for x in rows)
for row in rows:
 assert all(x['deletion_count']==row['deletions'] for x in row['outcomes'])
 row['registry_deletion_percent']=100*row['deletions']/4290
 row['all_three_reached20']=all(x['generations_completed']==20 and x['terminal_reason']=='reached_max_generations' for x in row['outcomes'])
 row['outcomes']=[{'simulation_seed':seed,'generations_completed':x['generations_completed'],'terminal_reason':x['terminal_reason'],'late_cycle_metrics':x.get('late_cycle_metrics'),'generation_durations_s':[g['generation_duration_s'] for g in x['generations']],'dry_mass_fold_changes':[g['final_dry_mass_fold_change'] for g in x['generations']],'solver_assisted_cells':sum(bool(g.get('solver_fallback_assisted')) for g in x['generations'])} for seed,x in zip(seeds,row['outcomes'])]
methods=['no_retrieval','frozen_dossier','adaptive_literature','greedy_block','randomized_block']
method_summary={m:{'assigned_searches':5,'all_three_reached20':sum(x['all_three_reached20'] for x in rows if x['method']==m),'terminal_counts':dict(Counter(o['terminal_reason'] for x in rows if x['method']==m for o in x['outcomes']))} for m in methods}
physical=[x for group in current_lines.values() for x in group.values()];baselines=[x for x in physical if not x['name'].endswith('control')];controls=[x for x in physical if x['name'].endswith('control')]
assert len(physical)==33 and len(baselines)==27 and len(controls)==6
result={'at':datetime.now(timezone.utc).isoformat(),'experiment_id':op.name,'science_status':'completed_with_exceptions','science_finished_at':max(v['files']['experiments.json']['value']['experiments'][0]['finished_at'] for v in current['workflows'].values()),'totals':{'active_workers':0,'active_cells':0,'completed_divisions':sum(x['generations_completed'] for x in physical),'lineages_at20':sum(x['generations_completed']==20 for x in physical),'physical_baseline_lineages':len(baselines),'baseline_terminal_counts':dict(Counter(x['terminal_reason'] for x in baselines)),'controls_at20':sum(x['generations_completed']==20 for x in controls)},'method_summary':method_summary,'comparison_rows':rows,'physical_baseline_division_range':{'nondivision':[min(x['generations_completed'] for x in baselines if x['terminal_reason']=='nondivision_max_duration'),max(x['generations_completed'] for x in baselines if x['terminal_reason']=='nondivision_max_duration')],'model_exception':[min(x['generations_completed'] for x in baselines if x['terminal_reason']=='model_exception'),max(x['generations_completed'] for x in baselines if x['terminal_reason']=='model_exception')]},'interpretation':['Nondivision means no next division within the preset 10800 simulated seconds, not demonstrated cell death.','Numerical/model exceptions remain inconclusive for biological viability; no repairs or reruns were performed.','Original agent results are reused unchanged; baseline selection and stopping were exploratory amendments, not an equal-budget superiority test.','Greedy349 and randomized349 are the same genotype and share three trajectories; they are not independent validation replicates.','The 1405-deletion greedy227 shortfall is retained. Other baseline nominees have1705–1720 total deletions.','Counts include the common1030 deletions. Percentages use4290 registry genes, not a model-gene denominator.','Twenty model generations are computational evidence, not laboratory viability.'],'source_sha256':{str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [op/'final-checkpoint-metadata.json',op/'matched-nominations.json',op/'selection-amendment.json',*[old/f'metadata-{seed}/progress.json' for seed in seeds]]},'analysis_method_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
(op/'final-outcome-summary.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
with (op/'comparison-outcomes.csv').open('w') as stream:
 fields=['method','search_seed','source_run_id','state_id','study','deletions','registry_deletion_percent','simulation_seed','generations_completed','terminal_reason'];w=csv.DictWriter(stream,fieldnames=fields);w.writeheader()
 for row in rows:
  for outcome in row['outcomes']:w.writerow({field:row[field] if field in row else outcome[field] for field in fields})
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'pdf.fonttype':42,'svg.fonttype':'none'})
fig,ax=plt.subplots(figsize=(11.5,14.7));fig.patch.set_facecolor('white');ax.set_xlim(0,11);ax.set_ylim(0,39);ax.axis('off')
fig.text(.06,.972,'Deletion depth and whole-cell outcomes',fontsize=20,weight='bold')
fig.text(.06,.947,'Three simulation seeds per genome; original agent results plus the exploratory matched baselines',fontsize=10,color='#475569')
ax.text(.1,37.5,'Search seed',weight='bold');ax.text(2.0,37.5,'Total genes deleted / 4,290',weight='bold');ax.text(7.65,38,'Completed divisions',ha='center',weight='bold')
for center,seed in zip([6.3,7.8,9.3],seeds):ax.text(center,37.45,str(seed),ha='center',weight='bold')
headings=[('No retrieval',['no_retrieval']),('Frozen dossier',['frozen_dossier']),('Adaptive literature',['adaptive_literature']),('Baselines',['greedy_block','randomized_block'])]
y=35.9;scale=3.5/2400;barx=2.0
for heading,group in headings:
 ax.text(.1,y,heading,fontsize=13,weight='bold',va='center');summaries=['%s %d/5'%({'greedy_block':'Greedy','randomized_block':'Randomized'}.get(m,''),method_summary[m]['all_three_reached20']) for m in group];ax.text(10.7,y,'; '.join(summaries)+' all 3 at 20',ha='right',va='center',fontsize=9,color='#475569');y-=1.0
 for method in group:
  for row in sorted([r for r in rows if r['method']==method],key=lambda r:r['search_seed']):
   label=('%s '%('G' if method=='greedy_block' else 'R') if method in ['greedy_block','randomized_block'] else '')+str(row['search_seed'])+('*' if method in ['greedy_block','randomized_block'] and row['search_seed']==349 else '')
   ax.text(.15,y,label,va='center',fontsize=10)
   ax.add_patch(Rectangle((barx,y-.2),1030*scale,.4,facecolor='#cbd5e1',edgecolor='none'))
   ax.add_patch(Rectangle((barx+1030*scale,y-.2),(row['deletions']-1030)*scale,.4,facecolor='#475569' if method!='randomized_block' else '#8b5e3c',edgecolor='none'))
   ax.text(5.65,y,f"{row['deletions']:,}",ha='right',va='center',fontsize=10)
   for center,o in zip([6.3,7.8,9.3],row['outcomes']):
    reason=o['terminal_reason'];n=o['generations_completed'];error=reason=='model_exception'
    if reason not in ['model_exception','nondivision_max_duration','reached_max_generations']:raise ValueError('Unexpected terminal outcome')
    fill='#fff0dc' if error else plt.cm.Blues(.08+.76*n/20)
    ax.add_patch(Rectangle((center-.49,y-.32),.98,.64,facecolor=fill,edgecolor='#c7a06a' if error else 'white',linewidth=.8))
    text=f'{n} E' if error else (f'{n} N' if reason=='nondivision_max_duration' else str(n))
    ax.text(center,y,text,ha='center',va='center',color='white' if not error and n>=14 else '#172033',weight='bold' if n==20 else 'normal')
   y-=.82
  if len(group)>1:y-=.25
 y-=.75
ax.text(.1,y,'Controls · current matched batch',fontsize=12,weight='bold');y-=.9
for name,label in [('mg1655-control','MG1655'),('starting-genome-control','Common root')]:
 found=[next(x for x in current_lines[seed].values() if x['name']==name) for seed in seeds];ax.text(.15,y,label,va='center');ax.text(5.65,y,f"{found[0]['deletion_count']:,}",ha='right',va='center')
 for center,line in zip([6.3,7.8,9.3],found):
  ax.add_patch(Rectangle((center-.49,y-.32),.98,.64,facecolor=plt.cm.Blues(.84),edgecolor='white'));ax.text(center,y,str(line['generations_completed']),ha='center',va='center',color='white',weight='bold')
 y-=.82
fig.text(.06,.064,'N: no further division within 10,800 simulated seconds.  E: model/numerical error; viability unresolved.',fontsize=9)
fig.text(.06,.046,'G = greedy; R = randomized. * Shared genotype and trajectories. Grey bars: 1,030 inherited deletions.',fontsize=9)
fig.text(.06,.028,'Exploratory selection ceiling: 1,720 deletions; greedy 227 retained at 1,405. No outcome-based replacements.',fontsize=9)
fig.subplots_adjust(left=.045,right=.98,top=.925,bottom=.09)
for suffix in ['png','pdf']:
 fig.savefig(op/('matched-comparison.'+suffix),dpi=180,facecolor='white')
plt.close(fig);(op/'source'/Path(__file__).name).write_bytes(Path(__file__).read_bytes());print(json.dumps({'totals':result['totals'],'method_summary':method_summary,'figure':str(op/'matched-comparison.png')},indent=2))
