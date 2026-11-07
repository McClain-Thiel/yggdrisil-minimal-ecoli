import json,hashlib,shutil
from pathlib import Path
from datetime import datetime,timezone
root=Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil')
out=root/'runs/literature-ablation-kimi-continuation-20261106'
a=json.load(open('/private/tmp/yggdrisil-functional-comparison-raw.json'))
b=json.load(open('/private/tmp/yggdrisil-current-functional-genes.json'))
old={x['ecocyc_id'] for x in a['mapping']};current={x['ecocyc_id'] for x in b['rows']};shared=old&current
reference={x['ecocyc_id'] for x in a['mapping'] if x['Gene'] in set(a['reference_deleted_symbols'])}
mapped={x['ecocyc_id'] for x in a['mapping'] if len(x['b_numbers'])==1}
assert (len(old),len(current),len(shared),len(mapped),len(reference))==(1219,1915,1197,1216,482)
assert not b['unmapped']
assert all({x['b_number'] for x in f['essential_deleted']}=={x['b_number'] for x in a['finalists'][1]['essential_deleted']} for f in a['finalists'][2:])

def metric(n,d):return {'deleted':n,'denominator':d,'percent':100*n/d,'retained':d-n}

mapping={'schema_version':1,'published_functional_rows':a['mapping'],'current_functional_rows':b['rows'],'shared_functional_ecocyc_ids':sorted(shared),'reference_deleted_ecocyc_ids':sorted(reference),'frozen_finalists':a['finalists']}
data=(json.dumps(mapping,separators=(',',':'))+'\n').encode();mp=out/'functional-gene-mapping.json';mp.write_bytes(data)
report={
 'schema_version':1,'analyzed_at':datetime.now(timezone.utc).isoformat(),
 'search_experiment_id':'literature-ablation-kimi-continuation-20261106',
 'validation_experiment_id':'vecoli-kimi-continuation-20gen-3seed-20261106',
 'analysis_type':'Read-only metadata/set analysis; no new simulations, fitting, model calls or robustness experiments.',
 'source':{
  'paper_doi':'10.1016/j.cels.2025.101392','repository':'https://github.com/ioanagherman/surrogateMinesweeperEcoli','commit':'47e3997bac61d481fa0e2fa0d48c1b6ca98762b0',
  'functional_gene_file':'WCM2022/wcm2022_submit/wcEcoli/runscripts/reflect/functional_genes.tsv','functional_gene_file_git_blob':'bdaa5f6b484a87e239ac16f424e7fb741af26943','functional_gene_file_sha256':'7f0763f31eb8b1c575d3f8a3765711a4a5a264952042801693b44240853af8c7',
  'gene_crosswalk_file':'WCM2022/wcm2022_submit/wcEcoli/reconstruction/ecoli/flat/genes.tsv','gene_crosswalk_sha256':'4f20fc0694b125f83658a308a3029c2bde9943830c624352c79f4702229179d6',
  'reference_design_notebook':'notebooks/plot_EMine737/Gene plots and ontology-public.ipynb','notebook_git_blob':'add051faab9dd5a8099a478226b88b1a925f4930','notebook_data_extraction':'AST literal parsing of all_genes_wcm and delted_minesweeper; notebook was not executed.',
  'native_commit':b['native_commit'],'registry_sha256':a['registry_sha256'],'frozen_validation_manifest_sha256':a['manifest_sha256'],'sim_data_sha256':b['sim_data_sha256'],
  'remote_root':'/home/ubuntu/yggdrisil-continuation-20261106'},
 'method':{
  'current_functional_rule':b['method'],
  'processes':['Metabolism catalysts and resolved protein subunits','Translation ribosomal proteins','Transcription RNAP subunits','RNA decay endo/exonucleases','Transcription regulation factors and resolved protein subunits'],
  'scope_limit':'This is the published five-process accounting rule applied consistently. It is not a claim of an exhaustive audit of every additional functional dependency in modern vEcoli.',
  'mapping':'Published RNA IDs mapped to EcoCyc gene IDs through authors\' pinned genes.tsv; joined to frozen canonical b-number registry and knockout manifest. Independent b-number and EcoCyc intersections agree. Current monomer cistron IDs mapped through pinned native genes.tsv.',
  'current_functional_gene_count':1915,'current_gene_mapping_unresolved':0,'published_functional_gene_count':1219,'published_mapping_to_registry_count':1216,'shared_functional_gene_count':1197,
  'published_entries_absent_from_registry':[{'symbol':x['Gene'],'ecocyc_id':x['ecocyc_id']} for x in a['unmapped']],
  'entry_count_note':'Published 1219-entry definition treats ilvG_1 and ilvG_2 separately. These two plus phnE_1 are absent from our search registry. Shared 1197-entry metric excludes all nonshared coverage without asserting they were experimentally deleted or retained.',
  'shared_set_rule':'Intersection of the exact published functional list and current five-process functional list, by stable EcoCyc ID; no inferred aliases.'},
 'published_reference':{
  'name':'Gherman et al. 2025 E. coli EMine737','own_published_functional_set':metric(482,1219),'registry_mappable_published_panel':metric(len(reference&mapped),1216),'shared_functional_set':metric(len(reference&shared),1197)},
 'finalists':[],
 'mapping_file':{'path':str(mp),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)},
 'validation_context':{'status':'In progress at analysis; this analysis changes no validation statuses. Known no-retrieval seed307 generation15 model exception remains distinct from biological nondivision.','interpretation':'Model-based deletion candidates; these fractions do not establish physical DNA reduction, laboratory viability or global minimality.'},
 'essentiality':{'annotation_source_doi':'10.1128/msystems.00896-22','essential_definition':'Author E calls in both LB and M9, validated against ecIPKM threshold by project parser.','deleted_annotation_essential_count_per_candidate':53,'inherited_from_common_start':True,'inside_current_functional_set':4,'outside_current_functional_set':49,'inside_published_functional_set':2,'interpretation':'The functional coverage gap means simulator survival alone does not resolve the experimental-essentiality discrepancy. This is a coverage audit, not a claim that the 49 genes have no biological function.'},
 'checks':['Published list exactly equals the 1219-entry list in authors\' analysis notebook.','All 482 reference deletions are contained in that list.','Current list has 1915 unique monomers, unique EcoCyc IDs and exactly one canonical b-number per gene.','All five frozen knockout sets give identical counts by independent b-number and EcoCyc intersections.','All three candidates inherit the exact same 53 essential-annotation deletions from the starting control.']}
for of,nf in zip(a['finalists'],b['finalists']):
 assert of['name']==nf['name']
 assert of['paper_functional_deleted']==of['paper_functional_direct_ecocyc_deleted']
 assert nf['functional_unique_genes_deleted']==nf['direct_ecocyc_deleted']
 report['finalists'].append({'name':of['name'],'state_id':of['state_id'],'deletion_set_sha256':of['deletion_set_sha256'],'full_registry':metric(of['deleted_count'],4290),'current_functional_set':metric(nf['functional_unique_genes_deleted'],1915),'published_1219_entry_panel':metric(of['paper_functional_deleted'],1219),'registry_mappable_published_panel':metric(of['paper_functional_deleted'],1216),'shared_functional_set':metric(len(set(of['vecoli_gene_ids'])&shared),1197),'current_functional_essential_deletions':nf['essential_deleted_functional']})
(out/'functional-gene-comparison.json').write_text(json.dumps(report,indent=2)+'\n')
for name in ['yggdrisil-compare-functional-genes.py','yggdrisil-current-functional-genes.py','yggdrisil-record-functional-comparison.py']:
 shutil.copyfile(Path('/private/tmp')/name,out/'source'/name)
print(json.dumps({'report':str(out/'functional-gene-comparison.json'),'mapping_bytes':len(data),'shared_reference':report['published_reference']['shared_functional_set']},indent=2))
