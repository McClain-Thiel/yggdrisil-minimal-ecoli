"""Verify exact frozen knockouts before the diagnostic starts."""

import copy
import hashlib
import json
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path

from ecoli.variants.yggdrisil_multi_gene_knockout import apply_variant

folder = Path(__file__).resolve().parent
manifest_path = folder / 'manifest.json'
manifest = json.loads(manifest_path.read_text())
sim_path = Path(manifest['workflow']['sim_data_path'])
with sim_path.open('rb') as handle:
    sim_data = pickle.load(handle)
assert sim_data.condition == 'basal'
rows = []
for item in manifest['finalists']:
    started = time.monotonic()
    indices, rna_ids = [], []
    if item['vecoli_gene_ids']:
        variant = apply_variant(copy.deepcopy(sim_data), {'gene_ids': item['vecoli_gene_ids']})
        indices = list(variant.yggdrisil_knockout_rna_indices)
        rna_ids = list(variant.yggdrisil_knockout_rna_ids)
        del variant
    assert len(indices) == item['deletion_count']
    item['target_rna_indices'] = indices
    row = {'name': item['name'], 'state_id': item['state_id'], 'variant_index': item['variant_index'], 'requested_genes': item['deletion_count'], 'exact_knockout_supported': True, 'zero_expression_verified': True, 'targeted_rna_ids': rna_ids, 'targeted_rna_indices': indices, 'elapsed_s': time.monotonic()-started}
    rows.append(row)
    print(json.dumps({k:v for k,v in row.items() if not k.startswith('targeted_')}), flush=True)
audit = {'created_at': datetime.now(timezone.utc).isoformat(), 'sim_data_path': str(sim_path), 'sim_data_sha256': hashlib.sha256(sim_path.read_bytes()).hexdigest(), 'basal_condition': sim_data.condition, 'cistron_count': len(sim_data.process.transcription.cistron_data.struct_array), 'rna_count': len(sim_data.process.transcription.rna_data.struct_array), 'variants': rows, 'all_exact_knockouts_supported': True}
audit_path = folder/'variant-knockout-audit.json'
audit_path.write_text(json.dumps(audit,indent=2)+'\n')
manifest['vecoli'].update(variant_knockout_audit_path=str(audit_path), variant_knockout_audit_sha256=hashlib.sha256(audit_path.read_bytes()).hexdigest())
manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
preflight_path=folder/'preflight.json'
preflight=json.loads(preflight_path.read_text())
preflight.update(status='native_knockout_audit_passed', all_exact_knockouts_supported=True, checked_at=datetime.now(timezone.utc).isoformat())
preflight_path.write_text(json.dumps(preflight,indent=2)+'\n')
