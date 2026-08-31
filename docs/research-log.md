# Research log

This file is the chronological research record for the genome-minimization
study. Protocols, frozen graphs, evaluator records, model traces, and checksums
remain the primary evidence. This log records the interpretation and the
decision made after each experiment.

## 2026-09-01 — Strong baselines and scheduler ablation

### Status

The first five paired seed blocks are complete for the canonical Sol and Qwen
arms, random search, evolutionary search, and the matched-cap Minesweeper
baseline. The first five Sol scheduler-ablation seed blocks are also complete.

### Observed

The primary endpoint is the maximum deletion count among states that pass both
the flux-balance analysis (FBA) gate and the final HiGHS
resource-balance-analysis (RBA) gate.

| Method | Seed results | Mean deletions |
| --- | --- | ---: |
| Random-uniform | 38, 34, 29, 46, 24 | 34.2 |
| Evolutionary-uniform | 72, 50, 72, 80, 60 | 66.8 |
| Matched-cap Minesweeper | 173, 111, 207, 174, 140 | 161.0 |
| Closed-book Sol | 233, 233, 211, 239, 268 | 236.8 |
| Closed-book Qwen | 239, 287, 246, 293, 252 | 263.4 |

Sol and Qwen exceeded matched-cap Minesweeper in all five paired seeds. The
mean paired improvements were 75.8 deletions for Sol and 102.4 deletions for
Qwen. Minesweeper exceeded the evolutionary and random baselines in all five
seeds.

The scheduler ablation changed only the scheduler from `recoverable` to
`frontier-only` for closed-book Sol:

| Seed | Frontier-only | Recoverable | Paired improvement |
| ---: | ---: | ---: | ---: |
| 101 | 20 | 233 | 213 |
| 202 | 40 | 233 | 193 |
| 303 | 8 | 211 | 203 |
| 404 | 0 | 239 | 239 |
| 505 | 0 | 268 | 268 |

The mean scheduler improvement was 223.2 deletions. The run-level paired
bootstrap 95% interval was 201.0 to 249.2 deletions. Recoverable scheduling won
in all five seeds. The exact two-sided paired sign-flip p-value was 0.0625.

Every scheduler graph passed SQLite integrity and foreign-key checks. Every
state had five evaluator records. The frontier-only runs used the final RBA
evaluator identity. The archived scheduler bundle has SHA-256
`6526366194d9eb24ccca23248d15643a62abc8f711d196aa407584b6be7daf29`.
The scheduler batch used USD 0.173545400 of authenticated OpenRouter account
usage.

Five older full-registry Sol finalists completed 20 of 20 generations at three
vEcoli lineage seeds each. This is 300 of 300 recorded divisions across 15
lineages. These are not the current 1,216-gene benchmark finalists, and the
result does not establish wet-lab viability.

### Inference

The first-five results support two separate conclusions.

- The agent methods find substantially deeper jointly feasible deletions than
  the strongest equal-budget structural baseline tested so far.
- Reopening viable non-leaf states prevents terminal frontier collapse and is
  a major contributor to search depth.

These conclusions are strong effect-size observations, but five paired runs are
not sufficient for the planned confirmatory inference. With five pairs, the
smallest possible two-sided exact sign-flip p-value is 0.0625. This limit is a
property of the sample size, not evidence that the observed effect is small.

### Decision

The study will extend the locked seed blocks symmetrically from five to ten for
the key comparisons. The additional runs are for independent replication,
variance estimation, and more stable confidence intervals. They are not an
optional continuation that stops when a target p-value is reached. The endpoint,
seeds `606, 707, 808, 909, 1010`, model settings, and analysis remain fixed in
advance. Every completed seed must be reported.

The next paid priorities are the remaining canonical Sol and Qwen seeds and the
one-factor gate and evidence-exposure ablations. The next free priority is to
extend the strongest baselines to the same seeds. Paid work requires an account
top-up and a new aggregate budget authorization; the existing protected
OpenRouter reserve must not be spent.

### Preserved evidence

- Strong-baseline archive:
  `runs/archive/wcm1216-strong-baselines-highs-seeds101-505-20260831.tar.gz`
- Sol/Qwen archive:
  `runs/archive/wcm1216-sol-qwen-highs-seeds101-505-20260830-first-five.tar.gz`
- Scheduler-ablation archive:
  `runs/archive/wcm1216-sol-scheduler-ablation-highs-seeds101-505-20260831.tar.gz`
- Local publication checklist: `docs/publication-experiment-checklist.md`
- Off-machine archive:
  <https://huggingface.co/buckets/McClain/Yggdrisil-ecoli-min>

The Hugging Face archive includes explanatory READMEs, frozen run bundles,
whole-cell outputs, scientific artifacts, provenance, and remote inventory
files. Raw third-party data with unclear redistribution rights is excluded;
source hashes and acquisition manifests are retained.

## 2026-09-01 — Strong-baseline seeds 606--1010

### Status

The evolutionary and matched-cap Minesweeper baselines are complete for all ten
locked seeds. The second-five block made no model or external API calls.

### Observed

The second-five jointly FBA-positive and final-HiGHS-RBA-feasible deletion
counts were:

| Seed | Evolutionary | Minesweeper |
| ---: | ---: | ---: |
| 606 | 64 | 117 |
| 707 | 54 | 133 |
| 808 | 59 | 95 |
| 909 | 89 | 141 |
| 1010 | 59 | 108 |

Across all ten seeds, evolutionary search found a mean of 65.9 deletions and
Minesweeper found a mean of 139.9 deletions. Minesweeper exceeded evolutionary
search in all ten pairs. The mean paired improvement was 74.0 deletions. The
run-level bootstrap 95% interval was 57.3 to 92.0 deletions. The exact
two-sided paired sign-flip p-value was 0.001953125.

All ten new graphs completed the 193-state budget, stored five evaluations per
state, passed SQLite integrity and foreign-key checks, and used final RBA
evaluator identity
`b75d336f1a247101fa0683592ca63eb62eaa386ee57a26a6d27486980d962681`.

### Inference

Matched-cap Minesweeper is a reproducibly stronger structural search baseline
than the evolutionary comparator under the common budget. The additional seeds
reduce uncertainty for this baseline comparison.

This result does not reduce the model-versus-Minesweeper p-values. Sol and Qwen
have not been run at seeds 606--1010. The model comparison remains a five-seed
descriptive result until those paired model arms are complete.

### Decision

Use Minesweeper, rather than random or evolutionary search, as the primary
non-agent baseline. Do not launch the paid second-five Sol and Qwen arms until
the OpenRouter account has enough balance for an aggregate guarded batch. Keep
the protected USD 1 reserve intact.

### Preserved evidence

- Run directory:
  `runs/strong-baselines/wcm1216-evolutionary-minesweeper-seeds606-1010-20260901`
- Frozen archive:
  `runs/archive/wcm1216-strong-baselines-highs-seeds606-1010-20260901.tar.gz`
- Archive SHA-256:
  `c03657c8eb5039c7be7f7ce5b0cec9e8eb69628a9d5b4e9d19919b1a3bbb31c2`
- Off-machine copy:
  `01-search-evidence/wcm1216-strong-baselines-highs-seeds606-1010-20260901.tar.gz`
  in the project Hugging Face bucket.

The off-machine archive was downloaded after upload and matched the local
SHA-256. Separate result and statistics files are also stored beside it in the
bucket.
