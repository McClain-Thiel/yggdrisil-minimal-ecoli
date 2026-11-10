import json,sqlite3,hashlib,time,sys
from pathlib import Path
from collections import Counter,defaultdict
from contextlib import closing
r=Path('/home/ubuntu/yggdrisil-replication-20261109');b=r/'bundle'
sys.path[:0]=[str(r/'application'),str(r/'application/src'),str(r/'framework/src')]
from scripts.collect_search_replication import decode_graph_value
jobs=json.loads((b/'jobs.json').read_bytes());root=json.loads((b/'inputs/starting-genotype.json').read_bytes()); rows=[];candidate_sets={}
for j in jobs:
 if j['method'] not in ['greedy_block','randomized_block']:continue
 p=b/j['directory'];raw=(p/'run-report.json').read_bytes();report=json.loads(raw);assert report['status']=='completed'
 candidates=report['candidates'];state_counts={root['state_id']:1030,**{x['state_id']:len(x['deleted_gene_ids']) for x in candidates}}
 with closing(sqlite3.connect((p/'graph.sqlite').as_uri()+'?mode=ro',uri=True)) as db:
  db.execute('BEGIN');assert db.execute('PRAGMA quick_check').fetchall()==[('ok',)]
  edges={e[1]:{'parent':e[0],'metadata':decode_graph_value(json.loads(e[2]))} for e in db.execute('SELECT parent_id,child_id,metadata_json FROM edges')}
  decisions=db.execute('SELECT count(*) FROM decisions').fetchone()[0]
 stats=defaultdict(Counter);parent_count=Counter();feasible=[];series=[]
 for c in candidates:
  assert c['evaluation_complete'];ev=c['evaluations'];fba=ev['fba']['metrics'];rba=ev['resource_allocation']['metrics'];fg=fba['feasible'] is True and fba['growth_rate']>0;rg=rba['feasible_at_growth_floor'] is True;ok=fg and rg
  edge=edges[c['state_id']];size=state_counts[c['state_id']]-state_counts[edge['parent']];bin=stats[str(edge['metadata']['block_size'])];bin.update(candidates=1,passed=int(ok),fba_failed=int(not fg),rba_failed=int(not rg));parent_count[str(state_counts[edge['parent']])]+=1
  if ok:feasible.append({'state_id':c['state_id'],'deletions':state_counts[c['state_id']],'growth':fba['growth_rate']})
  series.append({'candidate_index':c['candidate_index'],'added_genes':size,'parent_deletions':state_counts[edge['parent']],'total_deletions':state_counts[c['state_id']],'growth_gates_passed':ok})
 best=sorted(feasible,key=lambda x:(-x['deletions'],-x['growth'],x['state_id']))[0]
 rows.append({'run_id':j['run_id'],'method':j['method'],'seed':j['seed'],'report_sha256':hashlib.sha256(raw).hexdigest(),'source_policy_sha256':hashlib.sha256((r/'application/src/yggdrisil_ecoli/policies.py').read_bytes()).hexdigest(),'evaluated_candidates':len(candidates),'policy_decisions':decisions,'feasible_candidate_count':len(feasible),'feasible_candidates':feasible,'block_size_counts':dict(stats),'candidate_parent_deletion_counts':dict(parent_count),'finalist':best,'wall_time_s':report['wall_time_s'],'cpu_hours':report['cpu_hours'],'candidate_trajectory':series})
 candidate_sets[(j['method'],j['seed'])]={c['state_id'] for c in candidates}
pairs=[]
for seed in [103,227,349,461,587]:
 a=candidate_sets[('greedy_block',seed)];d=candidate_sets[('randomized_block',seed)]
 pair=[x for x in rows if x['seed']==seed]
 pairs.append({'seed':seed,'shared_evaluated_genotypes':len(a&d),'unique_union':len(a|d),'same_finalist':pair[0]['finalist']['state_id']==pair[1]['finalist']['state_id']})
print(json.dumps({'checked_at_unix':time.time(),'scope':'Read-only audit of completed baseline reports and graph edge metadata; no evaluations or searches executed.','runs':rows,'paired_methods':pairs}),flush=True)
