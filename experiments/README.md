# Experiment ledger

Start with the [agent versus baseline comparison](comparisons/agent-baselines-20261106.json), the [methods index](../methods/20261106/README.md), and the [complete ledger index](index.json). The index links each experiment to its configuration, exact scientific inputs, final results, W&B runs/artifacts, private S3 objects, checksums, failed attempts, and cleanup evidence.

## Verified comparison

| Search condition | Independent genomes | Registry genes deleted | Current functional genes deleted | 20-generation lineages |
| --- | ---: | ---: | ---: | ---: |
| Agent, no retrieval | 1 | 1,919/4,290 (44.73%) | 789/1,915 (41.20%) | 2/3; third reached 14 then model error |
| Agent, frozen dossier | 1 | 1,765/4,290 (41.14%) | 666/1,915 (34.78%) | 3/3 |
| Agent, adaptive literature | 1 | 1,823/4,290 (42.49%) | 664/1,915 (34.67%) | 3/3 |
| Randomized block baseline | 5 | See exact per-genome comparison | See exact per-genome comparison | 0/15 |
| Greedy block baseline | 5 | See exact per-genome comparison | See exact per-genome comparison | 0/15 |
| MG1655 and common starting-genome controls | 2 | 0 and 1,030 | 0 and 341 | 6/6 in each validation study |

All ten baseline genomes divided once in all three simulation seeds. Twenty-nine trajectories then reached the fixed 10,800-second cell limit without another division. One greedy-baseline trajectory raised `InvalidBoundaryError`; retain this as a model exception. The eight successful agent trajectories reached 20 divisions. The remaining agent trajectory also ended with a model exception.

These are expression-knockout model outcomes in aerobic M9 glucose, not physical DNA shortening or laboratory viability. The current functional denominator follows the recorded five-process rule; it is not an exhaustive census of every dependency in modern vEcoli. The comparison JSON also reports the exact 1,197-gene panel shared with the published reference.

Each agent condition has one search seed and an inherited search graph. Each baseline method has five search seeds, starting from the common 1,030-deletion genome. Baselines consumed 500 new evaluated states per run; the agent searches stopped on model budgets. Simulation seeds are repeated trajectories of the same genome. These runs do not establish a matched-budget, statistically replicated method advantage or an independent benefit from literature retrieval. Both validation studies used the same strict numerical fallback policy; assisted generations and exceptions remain recorded.

## Records and data locations

- [Agent search continuation](records/literature-ablation-kimi-continuation-20261106.json) → [agent vEcoli validation](records/vecoli-kimi-continuation-20gen-3seed-20261106.json).
- [Ten baseline searches](records/deletion-baselines-20261106.json) → [baseline vEcoli validation](records/vecoli-deletion-baselines-20gen-3seed-20261106.json).
- [Methods and frozen source files](../methods/20261106/README.md).
- [S3 verification receipt](verification/backup-check-20261107.json).

Git contains the source and compact records. Private S3 contains full graphs, trajectories, daughter states, inputs, detailed analyses, and immutable archive indexes. W&B artifact versions and checksums are recorded alongside S3 references. Failed attempts remain preserved. The dedicated workers and their disks were removed after independent backup verification; shared compute remains untouched.

The full working `experiments.json` remains available locally and in private S3. It is excluded from Git to avoid a growing monolithic data file. `index.json` plus `records/*.json` reconstructs that ledger without discarding fields or historical entries. To reconstruct it offline into a new file, run `python scripts/export_experiment_ledger.py restore --output /path/to/new-ledger.json`. The restore refuses to overwrite an existing file and checks every record's size and SHA256.

Rebuild the comparison from the verified compact final analyses with `python scripts/build_agent_baseline_comparison.py --output experiments/comparisons/agent-baselines-20261106.json`. This reads existing metadata only; it launches no science or API calls.
