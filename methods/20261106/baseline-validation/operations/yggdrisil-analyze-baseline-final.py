import hashlib
import json
import math
import re
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

OUT = Path('/private/tmp/yggdrisil-baseline-final-audit')
read = lambda p: json.loads(p.read_bytes())
audit = read(OUT/'archive-member-audit.json')
index = {f['path']: f for f in read(OUT/'final-index.json')['files']}
knockouts = read(OUT/'metadata/knockout-audit/variant-knockout-audit.json')
assert knockouts['all_exact_knockouts_supported']
assert len(knockouts['variants']) == 12
for variant in knockouts['variants']:
    assert variant['zero_expression_verified'] and variant['exact_knockout_supported']

def inspect_history(prefix):
    chunks = [(p, v) for p, v in audit['histories'].items() if p.startswith(prefix)]
    assert chunks, prefix
    chunks.sort(key=lambda pair: float(Path(pair[0]).stem))
    first, last = chunks[0][1]['first'], chunks[-1][1]['last']
    return {'rows': sum(v['rows'] for _, v in chunks), 'initial_dry_mass_fg': first['listeners__mass__dry_mass'], 'final_dry_mass_fg': last['listeners__mass__dry_mass'], 'first_global_time_s': first['global_time'], 'last_global_time_s': last['global_time'], 'final_history_dry_mass_fold_change': last['listeners__mass__dry_mass_fold_change'], 'finite_mass_time': True}

def window(gens, count):
    selected = gens[-count:]
    times = [g['generation_duration_s'] for g in selected]
    births = [g['initial_dry_mass_fg'] for g in selected]
    return {'generations': [g['generation'] for g in selected], 'mean_division_minutes': statistics.mean(times)/60, 'duration_cv': statistics.pstdev(times)/statistics.mean(times), 'birth_dry_mass_last_over_first': births[-1]/births[0], 'mean_dry_mass_fold_change': statistics.mean(g['final_dry_mass_fold_change'] for g in selected)}

lineages = []
parent_path = Path('/Users/mcclainthiel/Documents/ChatGPT/yggdrisil/runs/deletion-baselines-20261106/final/analysis.json')
parent = read(parent_path)
parent_by_state = {row['state_id']: row for row in parent['rows']}
for seed in [101, 211, 307]:
    label = f'seed-{seed}'
    progress = read(OUT/'metadata/runs'/label/'progress.json')
    assert hashlib.sha256((OUT/'metadata/runs'/label/'manifest.json').read_bytes()).hexdigest() == progress['manifest_sha256']
    output = f'runs/{label}/output/'+Path(progress['experiment_dir']).name
    manifest = read(OUT/'metadata/runs'/label/'manifest.json')
    for f in progress['finalists']:
        variant = f['variant_index']
        spec = next(s for s in manifest['finalists'] if s['variant_index'] == variant)
        assert len(set(spec['deleted_gene_ids'])) == f['deletion_count']
        assert spec['deletion_set_sha256'] == f['deletion_set_sha256']
        assert sorted(spec['target_rna_indices']) == sorted(knockouts['variants'][variant]['targeted_rna_indices'])
        item = {k:v for k,v in f.items() if k not in {'generations', 'terminal_task', 'late_cycle_metrics'}}
        item.update(seed=seed, deletion_percentage=100*f['deletion_count']/4290, registry_denominator=4290, target_rna_count=len(spec['target_rna_indices']), generations=[])
        if variant >= 2:
            search = parent_by_state[f['state_id']]
            assert search['denominators']['registry']['deleted'] == f['deletion_count']
            item.update(search_run_id=search['run_id'], search_method=search['method'], search_seed=search['seed'], deletion_denominators=search['denominators'])
        for gen in f['generations']:
            n = gen['generation']
            history_prefix = f'{output}/history/experiment_id={Path(output).name}/variant={variant}/lineage_seed={seed}/generation={n}/'
            measured = inspect_history(history_prefix)
            assert math.isclose(measured['final_dry_mass_fg'], gen['final_dry_mass_fg'], rel_tol=1e-12)
            daughter_prefix = f'{output}/daughter_states/variant={variant}/seed={seed}/generation={n}/'
            daughters = {Path(p).name:v['sha256'] for p,v in audit['daughters'].items() if p.startswith(daughter_prefix)}
            assert daughters == gen['daughter_state_sha256'], daughter_prefix
            assert len(daughters) == 2
            item['generations'].append({**gen, **measured, 'exact_target_transcripts_absent_in_both_daughters': True})
        assert len(item['generations']) == f['generations_completed']
        item['last4'] = window(item['generations'], 4)
        item['birth_dry_mass_last_over_first'] = item['generations'][-1]['initial_dry_mass_fg']/item['generations'][0]['initial_dry_mass_fg']
        item['solver_assisted_completed_generations'] = [g['generation'] for g in item['generations'] if g['solver_fallback_assisted']]
        if f['terminal_task']:
            task = f['terminal_task']
            stem = task['workdir'].removeprefix('/home/ubuntu/yggdrisil-baseline-validation-20261106/')
            error = audit['errors'][stem+'/.command.err']
            assert error['sha256'] == task['stderr_sha256']
            if f['terminal_reason'] == 'nondivision_max_duration':
                assert error['tail'].strip().endswith('TimeLimitError: Exceeded maximum simulation time: 10800.0')
            elif f['terminal_reason'] == 'model_exception':
                assert error['tail'].strip().endswith('wholecell.utils.modular_fba.InvalidBoundaryError: Negative molecule levels not allowed')
            else:
                raise RuntimeError('Unexpected terminal reason: '+f['terminal_reason'])
            hist = inspect_history(f'{output}/history/experiment_id={Path(output).name}/variant={variant}/lineage_seed={seed}/generation={task["generation"]}/')
            item['terminal_cell'] = {**task, **hist, 'stderr_last_line': error['tail'].strip().splitlines()[-1], 'classification': f['terminal_reason'], 'solver_fallbacks': [v for p,v in audit['solver_diagnostics'].items() if p.startswith(stem+'/')]}
        lineages.append(item)

for name, value in audit['solver_diagnostics'].items():
    d = value['data']
    assert d['model_unchanged'] and d['return_code'] == 0 and d['status'] == 'GLP_OPT: optimal', name
    assert d['tolerance_bound'] == d['tolerance_dual'] == 1e-7
    assert all(math.isfinite(x['relative_error']) and x['relative_error'] <= 1e-7 for x in d['kkt'].values())
    before = name.removesuffix('.json')+'.before.glp'
    after = name.removesuffix('.json')+'.after.glp'
    assert index[before]['sha256'] == index[after]['sha256'] == d['original_lp_sha256'], name
warnings = [{'path':p, **v} for p,v in audit['errors'].items() if v['messages']]
summary = {'lineages': len(lineages), 'baseline_lineages': 30, 'baseline_lineages_at_20': sum(x['variant_index']>=2 and x['generations_completed']==20 for x in lineages), 'baseline_divisions': sum(x['generations_completed'] for x in lineages if x['variant_index']>=2), 'baseline_terminal_reasons': dict(Counter(x['terminal_reason'] for x in lineages if x['variant_index']>=2)), 'control_lineages_at_20': sum(x['variant_index']<2 and x['generations_completed']==20 for x in lineages), 'completed_cycles_excluding_calibration': sum(x['generations_completed'] for x in lineages), 'calibration_completed_cycles': 2, 'solver_fallback_events_all_cells': len(audit['solver_diagnostics']), 'solver_assisted_completed_cells': sum(bool(g['solver_fallback_assisted']) for x in lineages for g in x['generations']), 'exact_target_transcripts_absent_all_304_daughters': True, 'finite_mass_time_in_all_2020_current_history_files': True}
assert len(lineages) == 36 and summary['baseline_terminal_reasons'] == {'nondivision_max_duration':29,'model_exception':1}
result = {'experiment_id':'vecoli-deletion-baselines-20gen-3seed-20261106', 'status':'completed_with_nondivision_and_model_exception', 'analyzed_at':datetime.now(timezone.utc).isoformat(), 'summary':summary, 'lineages':lineages, 'numerical_warning_logs':warnings, 'warning_counts':dict(Counter(m for w in warnings for m in w['messages'])), 'interpretation':'All ten frozen baselines completed exactly one division per seed; 29 of 30 then reached the fixed 10800-second cell cap without a second division, and greedy-block-seed-29 with simulation seed307 instead raised InvalidBoundaryError. Both controls reached20 generations for all three seeds. These are expression-knockout model outcomes under one environment, not measurements of physical genome deletion or laboratory viability. No scientific repair or rerun was introduced after observing these results.', 'comparison_limits':'Baseline genomes were selected using the frozen search oracle. A claim of agent superiority requires explicit alignment of search budgets, starting genomes, model-gene denominators, validation settings and solver policy; these results alone do not establish the contribution of literature retrieval or a mechanism for early division.'}
result['denominator_provenance'] = {'parent_analysis_sha256':hashlib.sha256(parent_path.read_bytes()).hexdigest(), 'functional_mapping':parent['functional_mapping'], 'joining_key':'exact frozen state_id', 'note':'Functional denominator counts are inherited from the independently verified baseline search analysis; target RNA counts and actual simulated knockout sets were rechecked separately.'}
(OUT/'final-analysis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(summary))
