import ast
import hashlib
import json
import math
import os
import re
import shlex
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

root = Path('/home/ubuntu/yggdrisil-continuation-20261106/validation')
read = lambda p: json.loads(p.read_bytes())
sha = lambda b: hashlib.sha256(b).hexdigest()
columns = ['global_time', 'listeners__mass__cell_mass', 'listeners__mass__dry_mass', 'listeners__mass__dry_mass_fold_change']

def inspect_history(paths):
    if not paths:
        raise RuntimeError('Missing emitted history')
    count = 0
    first = None
    last = None
    for path in sorted(paths, key=lambda p: float(p.stem)):
        rows = pq.ParquetFile(path).read(columns=columns).to_pylist()
        for row in rows:
            for key, value in row.items():
                if key == 'global_time' and value is None:
                    continue
                if value is None or not math.isfinite(value):
                    raise RuntimeError(f'Nonfinite {key} in {path}')
            if first is None:
                first = row
            last = row
            count += 1
    return {'rows_checked': count, 'initial_dry_mass_fg': first[columns[2]], 'final_dry_mass_fg': last[columns[2]], 'finite_mass_time_outputs': True}

def inspect_daughters(paths, targets):
    if len(paths) != 2:
        raise RuntimeError(f'Expected two daughters, got {len(paths)}')
    for path in paths:
        state = next(iter(read(path)['agents'].values()))
        fields = [f[0] for f in ast.literal_eval(state['unique_dtypes']['RNA'])]
        tu, active = fields.index('TU_index'), fields.index('_entryState')
        if any(row[active] and row[tu] in targets for row in state['unique']['RNA']):
            raise RuntimeError(f'Knocked-out transcript in {path}')
    return {p.name: sha(p.read_bytes()) for p in paths}

def metrics(gens, n):
    window = gens[-n:]
    times = [g['generation_duration_s'] for g in window]
    births = [g['initial_dry_mass_fg'] for g in window]
    return {'generations': [g['generation'] for g in window], 'mean_division_minutes': statistics.mean(times)/60, 'duration_cv': statistics.pstdev(times)/statistics.mean(times), 'birth_dry_mass_last_over_first': births[-1]/births[0], 'mean_dry_mass_fold_change': statistics.mean(g['final_dry_mass_fold_change'] for g in window)}

result = {'experiment_id': 'vecoli-kimi-continuation-20gen-3seed-20261106', 'checked_at': datetime.now(timezone.utc).isoformat(), 'original_lineages': [], 'interpretation': 'MG1655 expression-knockout projections in aerobic M9 glucose. Twenty divisions are model evidence, not laboratory viability. The same documented solver patch is applied to every genotype. Assisted cells are explicitly identified; no independent unpatched control is claimed.'}
for seed in (101, 211, 307):
    folder = root / 'runs' / f'seed-{seed}'
    manifest, progress = read(folder/'manifest.json'), read(folder/'progress.json')
    output = folder/'output'/f'vecoli_kimi_continuation_20gen_3seed_20261106_seed_{seed}'
    for lineage in progress['finalists']:
        spec = next(s for s in manifest['finalists'] if s['state_id'] == lineage['state_id'])
        targets = set(spec['target_rna_indices'])
        item = {k: lineage[k] for k in ('name','state_id','deletion_count','deletion_set_sha256','generations_completed','terminal_reason')}
        item.update(seed=seed, generations=[], target_rna_count=len(targets))
        for gen in lineage['generations']:
            n, variant = gen['generation'], lineage['variant_index']
            histories = list((output/'history'/f'experiment_id={output.name}'/f'variant={variant}'/f'lineage_seed={seed}'/f'generation={n}').rglob('*.pq'))
            daughter_files = list((output/'daughter_states'/f'variant={variant}'/f'seed={seed}'/f'generation={n}').rglob('daughter_state_*.json'))
            hashes = inspect_daughters(daughter_files, targets)
            if hashes != gen['daughter_state_sha256']:
                raise RuntimeError('Daughter hash differs from recorded generation')
            item['generations'].append({**gen, **inspect_history(histories), 'knockout_transcripts': 'passed'})
        item['solver_assisted_generations'] = [g['generation'] for g in item['generations'] if g.get('solver_fallbacks')]
        item['late_last5'] = metrics(item['generations'],5)
        item['late_last4'] = metrics(item['generations'],4)
        item['birth_dry_mass_last_over_first'] = item['generations'][-1]['initial_dry_mass_fg']/item['generations'][0]['initial_dry_mass_fg']
        result['original_lineages'].append(item)

warnings=[]
for name in ('seed-101','seed-211','seed-307'):
    folder=root/'runs'/name
    logs=list((folder/'output').rglob('.command.err')) if name.startswith('seed-') and 'recovery' not in name else list((folder/'cells').glob('*/stderr.log'))
    for path in logs:
        text=path.read_text(errors='strict')
        matches=[line.strip() for line in text.splitlines() if 'Warning:' in line or 'RuntimeError:' in line or 'TimeLimitError' in line or 'InvalidBoundaryError:' in line]
        if matches: warnings.append({'job':name,'path':str(path.relative_to(root)),'sha256':sha(path.read_bytes()),'messages':matches})
result['numerical_warning_logs']=warnings
result['warning_counts']=dict(Counter(line for w in warnings for line in w['messages']))
result['original_completed_cycles']=sum(x['generations_completed'] for x in result['original_lineages'])
result['lineages_at_20']=sum(x['generations_completed']==20 for x in result['original_lineages'])
result['solver_assisted_cells']=sum(bool(g.get('solver_fallbacks')) for x in result['original_lineages'] for g in x['generations'])
result['solver_fallback_count']=sum(len(g.get('solver_fallbacks',[])) for x in result['original_lineages'] for g in x['generations'])
result['all_accepted_daughter_transcript_checks']='passed'
result['all_accepted_mass_time_history_checks']='passed'
result['checked_at']=datetime.now(timezone.utc).isoformat()
assert len(result['original_lineages']) == 15
assert result['original_completed_cycles'] == 294 and result['lineages_at_20'] == 14
(root/'final-analysis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ('original_lineages','numerical_warning_logs')}))
