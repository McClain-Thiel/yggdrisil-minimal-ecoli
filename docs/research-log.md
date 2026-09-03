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

## 2026-09-01 — Ten-seed closed-book model comparison

### Status

The locked Sol, Qwen, and matched-cap Minesweeper comparison is complete for
all ten paired seeds `101, 202, 303, 404, 505, 606, 707, 808, 909, 1010`.
Every planned second-five model arm completed; the authenticated USD 29 guard
did not trigger.

### Observed

| Method | Ten-seed mean deletions | Range | Bootstrap 95% interval for mean |
| --- | ---: | ---: | ---: |
| Matched-cap Minesweeper | 139.9 | 95--207 | 120.1--161.0 |
| Closed-book Sol | 238.6 | 211--268 | 227.7--250.1 |
| Closed-book Qwen | 275.8 | 239--305 | 259.2--291.7 |

Sol exceeded Minesweeper in all ten pairs. Its mean paired advantage was 98.7
deletions with a run-level bootstrap interval of 69.9--125.6. Qwen also won all
ten pairs, by 135.9 deletions with an interval of 102.9--165.6. Both exact
two-sided paired sign-flip p-values are `0.001953125`; both Holm-adjusted
primary p-values are `0.00390625`.

Qwen exceeded Sol in eight of ten pairs. Its descriptive mean paired advantage
was 37.2 deletions (bootstrap interval 17.4--55.4; unadjusted exact
`p=0.01171875`). This was not a predeclared primary contrast.

The second-five arms used application commit
`8fea728af685ef0b036893e57e528a474028e203`, Yggdrisil commit
`67983c5c0821c57e6b0f60449b3e608b981455e2`, and final RBA evaluator
`b75d336f1a247101fa0683592ca63eb62eaa386ee57a26a6d27486980d962681`.
All ten new mutable/frozen graph pairs passed SQLite integrity, foreign-key,
logical-count, state-ID, and five-evaluations-per-state checks. The complete
twenty-model-run authenticated OpenRouter spend was USD 48.56144675.

### Inference

At the common 193-state budget, the closed-book agent methods find
substantially deeper jointly FBA-positive and RBA-feasible deletions than the
strongest structural baseline tested. The effect is consistent across seeds
and is large enough that neither primary result depends on an outlier.

This is a search-performance result, not proof of biological viability or a
new smallest genome. The deepest candidate deletes 305 of the 1,216 eligible
WCM-intersection genes. EMine-737 deletes 482 of 1,219 WCM genes and has direct
multi-generation WCM evidence. The current benchmark finalists have not yet
been simulated in vEcoli.

The first-five model trajectories used the preceding HiGHS evaluator identity.
The final revision only added retries for indeterminate status 4. Every stored
first-five RBA result was already classified `optimal` or `infeasible` by the
unchanged primary solve, and the selected primary states were independently
rescored feasible under the final evaluator. The stored policy-visible
feasibility decisions are equivalent; a free all-state rescore can document
that equivalence without repeating paid model calls.

### Decision

Use matched-cap Minesweeper as the paper's primary non-agent comparator. Keep
Qwen-versus-Sol descriptive. Measure the Minesweeper state-budget scaling
curve, replicate the gate and pure no-tool interventions, then predeclare
current finalists for vEcoli before reading any new WCM outcomes.

### Preserved evidence

- Second-five run directory:
  `runs/confirmatory/wcm1216-sol-qwen-highs-seeds606-1010-20260901`
- Frozen archive:
  `runs/archive/wcm1216-sol-qwen-highs-seeds606-1010-20260901.tar.gz`
- Archive SHA-256:
  `789513783a138e55dfd6f747c57f48c04c7c9301b5aa3349d4fdd14e9cb28302`
- Technical interpretation:
  `docs/technical-report-2026-09-01.md`
- Off-machine objects:
  `01-search-evidence/wcm1216-sol-qwen-highs-seeds606-1010-20260901.tar.gz`
  and the adjacent ten-seed result/statistics/integrity files in the project
  Hugging Face bucket.

The uploaded archive was downloaded after transfer and matched the local
SHA-256 exactly.

## 2026-09-02 — Minesweeper scaling launch and RBA status-4 correction

### Status

The predeclared ten-seed matched-cap Minesweeper scaling curve was launched
from clean graphs under final RBA evaluator identity
`c81ba9b529cb4e0c10cef545846671a24e23488d59c15d10ccbd62ed7497ae2a`.
Each deterministic trajectory has nested endpoints at 193, 500, 1,000, and
5,000 evaluated states. The experiment equalizes scientific-evaluator calls;
it does not equalize dollars, tokens, wall-clock time, or total computation.

### Numerical incident and correction

The first attempt was stopped when seed 505 reached a reproducible RBA linear
program for which the three configured HiGHS attempts returned status 4
(`Unknown`). A fourth documented path, `highs-ipm` without presolve, returned
an optimal solution. Independent residual checks found a maximum equality
residual of `5.31e-14`, a maximum inequality violation of `9.94e-8`, and no
bound violation; the inequality residual is within the pinned `1e-7`
feasibility tolerance. The fallback, evaluator-version increment, and a
real-artifact regression fixture were committed before starting the clean
panel. No state from the partial attempt is reused.

### Provisional interpretation

Four unaffected seeds completed 5,000 states before the first attempt was
halted. Their diagnostic deletion endpoints at 193, 500, 1,000, and 5,000
states were respectively `173/263/370/718`, `111/229/241/538`,
`207/304/407/714`, and `174/258/397/721`. These excluded diagnostics suggest
that the agents' advantage at 193 evaluations is a substantial
scientific-evaluation sample-efficiency result, while matched-cap Minesweeper
can catch up with hundreds to thousands of additional evaluator calls. No
final curve, interval, or crossing-point claim will use the superseded panel.

### Preservation

The superseded batch was checkpointed, integrity-checked, frozen as logical
SQLite backups, checksummed, archived, uploaded to the project Hugging Face
bucket under `04-audit-history`, downloaded again, and verified byte-for-byte.
Its archive SHA-256 is
`f8c651ce26c12e160b561580157e9e95c8a4f80432f1dd0b053cad3614ee3ccb`.
The clean panel retains a frozen protocol, source and artifact hashes,
environment validation, per-epoch logs, and a durable restart ledger while it
runs.

### Second numerical incident and version-4 continuation

The clean RBA-v3 panel later exposed a different deterministic status-4 state
in seed 505 after 1,776 evaluated states. All four automatic HiGHS paths were
indeterminate for that linear program. A final unscaled primal-simplex solve
(`highs-ds`, presolve disabled, simplex strategy 4, scaling strategy 0)
classified it as infeasible in about six seconds. A GLPK dual-simplex result
that claimed optimality was rejected because its independently computed
maximum row violation was `1.77e-4`, above the pinned `1e-7` tolerance; an
exact GLPK diagnostic did not finish and was terminated without being used.

The evaluator was incremented to version 4 with identity
`7fcfb3c034dddc9ce34d9c2b9b6d777a7a9100118788b8e22da2254d4ad10efd`
and the exact 335-deletion state was added as a regression fixture. Four seeds
(`101--404`) had already completed 5,000 states and contained only direct
definitive `optimal` or `infeasible` RBA records, so they remain eligible under
an explicit mixed-version audit. The affected or unstarted seeds
`505, 606, 707, 808, 909, 1010` were restarted from empty graphs under version
4; no partial graph was resumed or migrated.

The full second-incident directory, including all completed and partial raw
graphs, was compressed, uploaded to `04-audit-history`, downloaded, and
hash-verified. Its archive is
`wcm1216-minesweeper-states500-5000-seeds101-1010-rba-v3-second-incident-20260902.tar.zst`
with SHA-256
`35d8a6c7463c49153994fc2b35acee3c12896519654af46f2ed671163457d301`.
The four completed graphs were separately converted to integrity-checked,
read-only SQLite backups for the final mixed-version analysis. Local partial
database duplicates were removed only after the off-machine archive was
download-verified.

## 2026-09-02 — Ten-seed scheduler intervention

### Status

The predeclared recoverable-versus-frontier-only Sol scheduler comparison is
complete across all ten paired seeds. The second-five frontier-only block used
the same candidate universe, blinded evidence, model, action space, joint
FBA+RBA gate, and 193-state ceiling as the canonical reference. It spent USD
0.153662 of authenticated account usage.

### Observed

Frontier-only deletion endpoints across seeds 101--1010 were
`20, 40, 8, 0, 0, 0, 0, 30, 0, 0`; the paired recoverable endpoints were
`233, 233, 211, 239, 268, 241, 213, 268, 237, 243`. Recoverable scheduling won
all ten pairs. Its mean paired advantage was 228.8 deletions, with a
fixed-seed run-level bootstrap 95% interval of 215.6--242.0 and an exact
two-sided paired sign-flip `p=0.001953125`.

### Inference

Recovery is not a cosmetic scheduler detail in this benchmark. Restricting
the otherwise identical model policy to structural frontier leaves usually
terminates after one or two lethal expansions, whereas the recoverable open
set can revisit viable parents and continue searching. This is an algorithmic
intervention, not an independent biological-validity result.

### Preservation

- Local archive:
  `runs/archive/wcm1216-sol-scheduler-ablation-seeds606-1010-rba-v3-20260902.tar.gz`
- SHA-256:
  `671019f736925d1bf24dc56e47a717aa2f06db814d7b5da7b7ce7e3c42842d72`
- Off-machine copy:
  `01-search-evidence/wcm1216-sol-scheduler-ablation-seeds606-1010-rba-v3-20260902.tar.gz`

The uploaded archive was downloaded and verified byte-for-byte.

## 2026-09-02 — Viability-gate and blinded tool-access interventions

### Viability gate

Five Sol seeds changed only the search-time gate from joint FBA+RBA to
FBA-only. Under the common joint endpoint, the reference found
`233, 233, 211, 239, 268` deletions and FBA-only search found
`65, 60, 50, 9, 25`. Joint-gate search won all five pairs by a mean of 195.0
deletions (paired-run bootstrap 95% interval 166.2--226.4; exact two-sided
sign-flip `p=0.0625`). The FBA-only arm's own deeper endpoints
`412, 401, 414, 421, 393` all failed RBA. The result supports
resource-allocation feedback as trajectory-shaping evidence rather than a
post-hoc label.

The five arms spent USD 20.22001310. Their complete directory and an archive
were uploaded and download-verified; the archive SHA-256 is
`337aee5a3dbd67c9901405eef36f7405cd9b27b8f36faa175d3ab457299a441f`.

### Blinded aggregate bundle-analysis tool

Five Sol seeds preserved the canonical seed-specific blinded categorical
preview but removed every callable scientific tool. The canonical tool-enabled
arm found `233, 233, 211, 239, 268` deletions; no-tool search found
`193, 168, 164, 175, 186`. Tool-enabled search won all five pairs by a mean of
59.6 deletions (paired-run bootstrap 95% interval 46.4--71.8; exact two-sided
sign-flip `p=0.0625`). Every graph passed SQLite, evaluator-identity,
five-evaluations-per-state, blinded model-I/O, empty tool-list, and
zero-scientific-tool-call audits.

The panel spent USD 8.19110450. Its full 55-file directory was uploaded to
`01-search-evidence/ablations`, downloaded, and all declared file hashes were
verified. The compressed audit archive was also download-verified at SHA-256
`fdaabcf6ce29294b5adc2c110d77adf939029b18b2b7fb3538af90a3773e3b12`.

Both five-run interventions are descriptive mechanism evidence. At `n=5`, the
minimum possible two-sided exact paired sign-flip p-value is 0.0625; neither is
presented as a standalone confirmatory biological test.

## 2026-09-02 — Current-finalist vEcoli predeclaration

### Status

Six current WCM-universe primary candidates were selected before reading any
new whole-cell outcomes: Sol and Qwen at search seeds 101, 202, and 303. The
selection rule is the locked common primary endpoint. All six canonical
deletion sets map one-to-one into vEcoli with zero unmapped genes. The
selection hash is
`fd5e0248895a122c93d6278ab378be34d385cf6a5f2a3848c87a2450f1de4cce`.

Each candidate is predeclared for 20 generations at lineage seeds 101, 202,
and 303: 18 lineages and at most 360 divisions. No new vEcoli result existed
when the manifests and workflows were frozen. The simulation panel remains
pending while the search-scaling and paid ablation jobs use the host.

### Preservation and local storage

The predeclaration archive has SHA-256
`22c7272e93c4be269908cc7a9125866d1899a5fb00bb52ffacaf6f12f5db555b`
and was uploaded under `03-vecoli-validation`, downloaded, and verified.

Before reclaiming local disk, every 5,006 retained raw files from the older
seed-202/303 WCM panels passed the SHA-256 manifest downloaded from the bucket.
For the older seed-101 collection, a new 3,070-file manifest was verified
locally, uploaded, downloaded, and compared exactly; its SHA-256 is
`69ff87b47c0cfb43c89e4d3b8a40773d7b123610d04b8983e3f0a9567ed0b125`.
Only the verified old local raw `output/` duplicates were removed. Local
summaries, manifests, provenance, source graphs, checksums, and the `simData`
input required by the new workflows remain. The removed raw outputs are
recoverable from `02-whole-cell-validation`; the exact reclamation record is
also mirrored under `03-vecoli-validation`.

## 2026-09-03 — Final ten-seed Minesweeper scaling curve

### Completion and integrity

The predeclared scientific-evaluation scaling curve is complete. Four accepted
RBA-v3 trajectories (seeds 101--404) and six clean RBA-v4 trajectories (seeds
505--1010) each reached 5,000 states. Every graph contains 4,999 edges, 25,000
evaluator records, 1,250 decisions, and 5,000 proposal events. SQLite
integrity and foreign-key checks passed, all RBA outcomes were definitive, and
every 193-state prefix exactly matched its previously frozen matched-cap
baseline.

The final analyzer audits the elapsed time of each bounded Runner invocation,
not the calendar span of a trajectory resumed across several invocations. All
30 recorded epochs respected the 12-hour per-invocation limit; the longest
epoch was 10,933.96 seconds. This corrected an analysis-only rejection caused
by applying the 12-hour limit to the overall multi-epoch calendar span. The
graphs and scientific outcomes were unchanged.

### Results

The mean maximum jointly FBA-positive and RBA-feasible deletion depth was
139.9 at 193 states, 239.5 at 500, 324.7 at 1,000, and 653.8 at 5,000. The
run-level 95% bootstrap intervals were 119.9--161.4, 218.1--262.0,
279.3--367.6, and 605.3--697.9, respectively.

At 193 evaluations, Minesweeper lost all ten paired comparisons to both Sol
and Qwen. At 500 evaluations, it was nearly equal to Sol in the aggregate
(`+0.9` deletions; 5/10 paired wins) and still trailed Qwen by 36.3 deletions
(3/10 wins). At 1,000 evaluations, it exceeded the fixed 193-state Sol and
Qwen endpoints by 86.1 and 48.9 deletions on average. At 5,000 evaluations, it
exceeded both endpoints in all ten seeds.

Minesweeper first matched the paired Sol endpoint after a mean of 642.2
evaluations (median 482; range 196--1,709) and the paired Qwen endpoint after a
mean of 930.5 evaluations (median 767; range 250--2,351). The six-seed RBA-v4-
only sensitivity means were 122.3, 223.5, 305.3, and 635.7 deletions at the
four prefixes, preserving the same monotonic interpretation.

This result narrows the central paper claim. The closed-book agents are more
sample efficient under the fixed 193-state scientific-evaluation budget, but
the matched structural baseline can catch them with hundreds to thousands of
additional evaluations. The experiment does not equalize dollars, tokens,
wall-clock time, or total computation and therefore does not establish general
computational efficiency.

### Preservation

`FINAL_SHA256SUMS` covers 137 durable files. The final 4.71 GiB evidence tree
was compressed to a 108 MiB Zstandard archive, tested locally, uploaded under
`01-search-evidence`, downloaded into a fresh temporary directory, hash-
verified, and tested again. The archive is
`wcm1216-minesweeper-scaling-193-5000-ten-seed-rba-v3-v4-20260903.tar.zst`;
its SHA-256 is
`806559aa6c37e1109946808a54df746aa6b359054f3b68c967badff23f3eab8b`.
