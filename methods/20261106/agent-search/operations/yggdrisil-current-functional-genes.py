import json,pickle,hashlib
from pathlib import Path
import pandas as pd
r=Path('/home/ubuntu/yggdrisil-continuation-20261106')
sp=r/'validation/inputs/simData.cPickle'
with sp.open('rb') as h:s=pickle.load(h)
p=s.process
valid=set(p.translation.monomer_data['id'])
cx=set(p.complexation.complex_names)
eq=set(p.equilibrium.complex_name_to_rxn_idx)
tc=p.two_component_system.complex_to_monomer

def expand(ids,include_two_component=False):
 out=[]
 for x in ids:
  if x in cx:out.extend(p.complexation.get_monomers(x)['subunitIds'].tolist())
  elif x in eq or (include_two_component and x in tc):
   sub=p.equilibrium.get_monomers(x)['subunitIds'].tolist() if x in eq else tc[x]
   for y in sub:
    if y in cx:out.extend(p.complexation.get_monomers(y)['subunitIds'].tolist())
    elif y in valid:out.append(y)
  else:out.append(x)
 return set(out)&valid

groups={
 'Metabolism':expand(p.metabolism.catalyst_ids),
 'Translation':expand([s.molecule_ids.s30_full_complex,s.molecule_ids.s50_full_complex]),
 'Transcription':expand([s.molecule_ids.full_RNAP]),
 'RNA Decay':set(p.rna_decay.endoRNase_ids)|set(s.molecule_groups.exoRNases),
 'Transcription Regulation':expand([x+'[c]' for x in p.transcription_regulation.tf_to_tf_type],True),
}
lines=[x for x in (r/'native-validated/reconstruction/ecoli/flat/genes.tsv').read_text().splitlines() if not x.startswith('#')]
header=[json.loads(x) for x in lines[0].split('\t')]
ng=[dict(zip(header,[json.loads(x) for x in ln.split('\t')])) for ln in lines[1:]]
byrna={rid:g for g in ng for rid in g['rna_ids']}
md=p.translation.monomer_data
byprotein={x['id'][:-3]:str(x['cistron_id']) for x in md}
g=pd.read_parquet(r/'application/data/processed/genes.parquet')
reg_ec={}
for idx,row in g.iterrows():
 if isinstance(row['ecocyc_id'],str):reg_ec.setdefault(row['ecocyc_id'],[]).append(idx)
rows=[]
for mid in sorted({x[:-3] for values in groups.values() for x in values if x in valid}):
 rid=byprotein[mid];gene=byrna[rid];bn=set(reg_ec.get(gene['id'],[]))
 if not bn:bn={x for x in gene['synonyms'] if x in g.index}
 rows.append({'symbol':gene['symbol'],'ecocyc_id':gene['id'],'rna_id':rid,'monomer_id':mid,'b_numbers':sorted(bn),'processes':[k for k,v in groups.items() if any(x[:-3]==mid for x in v)]})
func_ec={x['ecocyc_id'] for x in rows}
m=json.loads((r/'validation/manifest.json').read_text())
report={'method':'Gherman 2025 five-process functional-accounting algorithm applied to existing current simData; no fitting or simulation. Not claimed exhaustive for additional vEcoli processes.','sim_data_sha256':hashlib.sha256(sp.read_bytes()).hexdigest(),'native_commit':'b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc','functional_monomer_count':len(rows),'functional_unique_gene_count':len(func_ec),'rows':rows,'unmapped':[x for x in rows if len(x['b_numbers'])!=1],'finalists':[]}
for f in m['finalists']:
 ds=set(f['deleted_gene_ids']);ves=set(f['vecoli_gene_ids'])
 hit=[x for x in rows if set(x['b_numbers'])&ds]
 hit_direct=[x for x in rows if x['ecocyc_id'] in ves]
 essential=[{'b_number':x,'symbol':str(g.loc[x,'symbol']),'ecocyc_id':str(g.loc[x,'ecocyc_id'])} for x in sorted(ds) if str(g.loc[x,'classification'])=='essential']
 report['finalists'].append({'name':f['name'],'functional_monomers_deleted':len(hit),'functional_unique_genes_deleted':len({x['ecocyc_id'] for x in hit}),'direct_ecocyc_deleted':len(hit_direct),'essential_deleted_functional':[x for x in essential if x['ecocyc_id'] in func_ec]})
print(json.dumps(report,indent=2))
