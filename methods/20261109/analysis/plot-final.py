import json,os
from pathlib import Path
os.environ['MPLCONFIGDIR']='/private/tmp/yggdrisil-plot-mpl'
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
R=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil');data=json.loads((R/'experiments/comparisons/search-replication-20261109.json').read_bytes());order={'adaptive_literature':0,'frozen_dossier':1,'no_retrieval':2,'greedy_block':3,'randomized_block':3,'control':4};labels={'adaptive_literature':'Adaptive','frozen_dossier':'Frozen dossier','no_retrieval':'No retrieval','greedy_block':'Both baselines','randomized_block':'Both baselines'};colors=['#197c62','#ad6a25','#4e6a9c','#807b78','#555555']
def key(r):
 s=r['search_runs'];return (order[s[0]['method']],s[0]['seed']) if s else (4,r['deletions'])
rows=sorted(data['genotypes'],key=key);fig,(a,b)=plt.subplots(1,2,figsize=(8.8,10.2),gridspec_kw={'width_ratios':[3.7,1.7]});fig.subplots_adjust(left=.23,right=.96,top=.88,bottom=.22,wspace=.3);n=len(rows)
for i,r in enumerate(rows):
 src=r['search_runs'];method=src[0]['method'] if src else 'control';c=colors[order[method]];label=(labels[method]+' '+str(src[0]['seed'])) if src else ('MG1655' if r['deletions']==0 else 'Starting genome')
 if src and not src[0]['full_evaluation_budget_reached']:label+='*'
 base=min(1030,r['deletions']);a.barh(i,base,height=.63,color='#d7d7d7');a.barh(i,r['deletions']-base,left=base,height=.63,color=c);a.text(r['deletions']+35,i,str(r['deletions']),va='center',fontsize=8)
 for j,line in enumerate(r['lineages']):
  k=line['failure_category'];code={'observed_nondivision':'N','software_error_handling_path':'P','unresolved_model_or_numerical_failure':'M'}.get(k,'');text=str(line['generations_completed'])+code
  bg='#d9eee6' if line['primary_lineage_passed'] else '#faf0de' if code=='N' else '#e8e7ef';b.add_patch(Rectangle((j-.43,i-.36),.86,.72,facecolor=bg,edgecolor='white'));b.text(j,i,text,ha='center',va='center',fontsize=9,fontweight='bold' if line['primary_lineage_passed'] else 'normal')
 a.text(-.03,i,label,ha='right',va='center',transform=a.get_yaxis_transform(),fontsize=8)
for ax in [a,b]:
 ax.set_ylim(n-.4,-.8);ax.set_yticks([])
 for spine in ax.spines.values():spine.set_visible(False)
a.set_xlim(0,2650);a.set_xticks([0,1000,2000]);a.grid(axis='x',alpha=.15);a.set_axisbelow(True);a.set_xlabel('Deleted registry genes',fontsize=9);a.set_title('Deletion depth',loc='left',fontsize=11)
b.set_xlim(-.5,2.5);b.set_xticks([0,1,2],['101','211','307']);b.set_xlabel('Simulation seed',fontsize=9);b.set_title('Completed divisions',loc='left',fontsize=11)
fig.text(.05,.95,'Genome deletion and whole-cell persistence',fontsize=16,weight='bold');fig.text(.05,.917,'Final replication • 20 unique candidate genomes + 2 controls • 927 divisions',fontsize=10)
fig.text(.05,.04,'20 = reached the endpoint   N = nondivision at the cell-time cap\nP = Python error-handling path   M = unresolved model/numerical exception\nGray bars = common 1,030-deletion starting genome; color = additional deletions.\n* Search ended before 100 evaluations. Both baselines share identical genotypes.\nModel evidence only: exceptions are not proof of biological death.',fontsize=8,linespacing=1.6)
out=R/'experiments/figures';out.mkdir(exist_ok=True);fig.savefig(out/'search-replication-20261109.png',dpi=110,facecolor='white');plt.close(fig);print((out/'search-replication-20261109.png').stat().st_size)
