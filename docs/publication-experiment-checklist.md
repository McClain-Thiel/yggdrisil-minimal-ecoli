# Publication experiment checklist

This is the prospective checklist for confirmatory runs started after the
resource-allocation solver was changed from GLPK to SciPy HiGHS. Earlier runs
remain useful exploratory evidence, but they do not count toward the fresh
replication totals below.

## What is already established

- [x] Five older closed-book Sol finalists from the full 4,290-gene search were
  simulated with pinned vEcoli.
- [x] Each finalist completed 20 generations at lineage seeds 101, 202, and
  303: 15 lineages and 300/300 successful divisions.
- [x] WT and `lacZ` controls divided; the essential `rpsT` control failed in
  generation one. The `thrA` control ended in a recorded model exception and
  was not classified as biological death.
- [x] The five WCM-tested deletion sets pass the corrected FBA plus HiGHS-RBA
  gate. RBA covers only 4--11 of their 1,470--1,597 deletions, so the direct
  vEcoli result is the important terminal evidence for these genotypes.
- [x] Five corrected HiGHS random-uniform and heuristic-uniform baseline seeds
  are complete.
- [x] Every final graph is frozen separately and stores evaluator, artifact,
  policy, model, prompt, seed, cost, and source provenance.

The vEcoli result supports sustained division in one pinned whole-cell-model
configuration. It does not establish wet-lab viability, environmental
robustness, or correctness outside model coverage. It also applies to the older
full-registry Sol finalists, not the newer 1,216-gene Sol/Qwen benchmark
candidates.

## Numerical correction

The correction was to the RBA fixed-growth evaluator, not to vEcoli. GLPK
reported six seed-202 states as feasible even though their returned solutions
violated constraints by approximately 0.0093--0.0095. It also showed
pathological solve times on some genotypes. The production evaluator now uses
SciPy HiGHS with presolve and explicit `1e-7` primal and dual feasibility
tolerances.

- [x] Audit the same 193 states with GLPK and HiGHS.
- [x] Preserve the six false-positive state IDs and residuals.
- [x] Re-run the environmental and RBA-discriminator panels.
- [x] Re-run all free uniform baselines under evaluator ID
  `130077fc55e8d98ff558c10907f8b803fbb3b27151a37f170be79cef7f55993d`.
- [x] Retrospectively rescore the saved Sol and Qwen trajectories.
- [x] Re-run the first five paired Sol and Qwen seeds from scratch because
  post-hoc rescoring cannot repair parent selection and feedback from the old
  gate. The second five remain pending.
- [x] Add a provenance-recorded HiGHS interior-point/no-presolve fallback after
  a matched-cap Minesweeper state reproducibly returned automatic-method status
  not set. The exact state is a regression test; unresolved LPs still fail.

Retrospective rescoring changes the newer WCM-universe seed-101 headline from
190 to 189 joint-feasible deletions for Sol and from 220 to 205 for Qwen. These
are exploratory corrected candidates, not confirmatory run results.

## Locked experimental unit and configuration

One complete from-scratch search run is the experimental unit. Evaluated
states, proposals, genes, and serial WCM generations are not independent
replicates.

- [ ] Use the ten locked seed blocks `101, 202, 303, 404, 505, 606, 707, 808,
  909, 1010` for every confirmatory arm. Seeds 101--505 are complete for Sol,
  Qwen, random-uniform, and heuristic-uniform.
- [x] Run the first five seeds as a budget checkpoint, but commit in advance to
  all ten for confirmatory inference; do not stop because interim results are
  favorable or unfavorable.
- [x] Interleave model order within each seed block to limit provider
  drift and calendar-time effects.
- [x] Use a fresh SQLite graph for every first-five model arm and seed. Never
  use `--new-run` as a substitute for an independent graph.
- [x] Freeze the application and framework commits, evaluator identities,
  artifact hashes, candidate-universe hash, prompt version, model/provider ID,
  tool manifest, and all limits before the first-five launch.
- [x] Run first-five paid calls serially and enforce an authenticated
  account-level budget cap. Response-reported costs are secondary audit data.

Canonical agent configuration:

- candidate universe: `wcm-1219` (1,216 mapped genes)
- maximum action size: 20, model-selected from 1--20
- four proposals per selected parent (up to 16 per step), open-set width 16,
  four parents per step
- 193 states, 48 steps, and 7,200 seconds maximum wall time
- at most six model requests and 16 tool calls per explorer invocation, with an
  8,000-output-token cap per model request
- closed-book identifiers and evidence
- recoverable scheduler
- joint FBA-positive plus HiGHS-RBA-feasible expansion gate

Use equal search/state/token opportunities for model comparisons. Record cost
and wall time as outcomes rather than using them to give one method more search.

## Spending tranches

These are planning reserves based on the exploratory seed-101 costs, not
guaranteed prices. Each tranche requires a new explicit authorization and an
account-level stop guard.

- [x] Tranche A: first five fresh Sol plus Qwen seed pairs. All ten arms
  completed for USD 24.4075 of authenticated account usage.
- [ ] Tranche B: first five Sol scheduler plus gate ablation pairs; reserve
  approximately USD 30 total.
- [ ] Tranche C: first five Sol action-size plus evidence-exposure pairs; reserve
  approximately USD 45 total.
- [ ] Authorize the second five seeds symmetrically only after the first-five
  checkpoint; never extend only the favorable arm.

## Phase 1: free controls

- [x] Random-uniform, seeds 101--505.
- [x] Heuristic-uniform, seeds 101--505.
- [ ] Extend random-uniform to seeds 606--1010.
- [ ] Extend heuristic-uniform to seeds 606--1010.
- [ ] Run random-fixed20 at seeds 101--1010.
- [ ] Run heuristic-fixed20 at seeds 101--1010.
- [x] Run evolutionary-uniform at seeds 101--505 under the common 193-state
  budget.
- [x] Run matched-cap Minesweeper at seeds 101--505 under the common 193-state
  budget.
- [ ] If matched-cap Minesweeper is competitive, predeclare and run a free
  500/1,000/5,000-state scaling curve rather than spending additional LLM
  budget first.

The heuristic's `no_proposals` termination after a lethal frontier child is a
method outcome, not a failed run.

At the first-five strong-baseline checkpoint, evolutionary search found 50--80
jointly feasible deletions (mean 66.8) and matched-cap Minesweeper found
111--207 (mean 161.0), versus random's 24--46 (mean 34.2). Minesweeper beat
evolutionary and random in all five paired seeds, but Sol (211--268; mean
236.8) and Qwen (239--293; mean 263.4) beat Minesweeper in every seed. The
mean Sol-minus-Minesweeper and Qwen-minus-Minesweeper effects were 75.8 and
102.4 deletions. These remain descriptive at `n=5`; every exact two-sided
paired sign-flip p-value is 0.0625.

All ten final strong-baseline graphs contain exactly 193 states and five final
evaluator records per state. The 15 prior Sol/Qwen/random primary states were
re-evaluated with final RBA evaluator
`b75d336f1a247101fa0683592ca63eb62eaa386ee57a26a6d27486980d962681`;
all remained feasible without fallback. The frozen strong-baseline bundle is
`runs/archive/wcm1216-strong-baselines-highs-seeds101-505-20260831.tar.gz`
(SHA-256 `fb51760f9cabf632787c331ff3e9e4bf2a85fabec7cb243d54340711e884f762`).

## Phase 2: corrected model comparison

- [x] Fresh `openai/gpt-5.6-sol` canonical runs at seeds 101--505.
- [x] Fresh `qwen/qwen3.6-35b-a3b` canonical runs at seeds 101--505.
- [ ] Fresh `openai/gpt-5.6-sol` canonical runs at all ten seeds.
- [ ] Fresh `qwen/qwen3.6-35b-a3b` canonical runs at all ten seeds.
- [x] Analyze the first five complete paired seed blocks before authorizing the
  second five, without changing endpoints, prompts, or stopping rules.

At the first-five checkpoint, jointly feasible deletion counts were
211--268 for Sol (mean 236.8) and 239--293 for Qwen (mean 263.4), versus
24--46 for paired random-uniform runs (mean 34.2). Both model arms exceeded
random in all five seed blocks. The mean paired differences were 202.6 genes
for Sol and 229.2 for Qwen; the respective run-level bootstrap 95% intervals
were [188.0, 224.4] and [212.8, 245.6]. Exact two-sided p-values are 0.0625
before and 0.125 after Holm adjustment, and remain explicitly
non-confirmatory at five seeds.
The frozen bundle is
`runs/archive/wcm1216-sol-qwen-highs-seeds101-505-20260830-first-five.tar.gz`
(SHA-256 `852805895077ab937b2af3edd16e671db56858bf89830d4839b8420437c50f9e`).

The primary endpoint is the deletion count of the most-deleted state passing
the common final FBA plus HiGHS-RBA gate. Break ties by higher FBA growth, then
lexicographically smaller state ID.

## Phase 3: core Sol ablations

Run every arm at the same ten seeds and change exactly one factor from canonical
Sol.

- [ ] **Scheduler:** `recoverable` versus `frontier-only`. This tests reopening
  viable non-leaf states after lethal or empty branches.
- [ ] **Gate:** `fba-rba` versus `fba-only`. Evaluate RBA on every state in both
  arms. Use the common joint gate for the cross-arm primary endpoint and report
  the FBA-only winner separately.
- [ ] **Action size:** `variable-1-max` versus `fixed-max`. This tests whether
  smaller recovery moves matter.
- [ ] **Evidence exposure:** `closed-book` versus `tool-rich`. This changes both
  identifier/evidence exposure and available tools, so label it an
  evidence-exposure ablation rather than a pure tool-use effect.
- [ ] Implement and test an optional `closed-book-no-tools` mode before claiming
  a pure tool-use effect. It must preserve the same blinded preview and remove
  only bundle-analysis tool access.

Candidate universe (`wcm-1219` versus all 4,290 genes) is a separate
generalization study, not a clean one-factor ablation, because it changes the
scientific task and action space.

## Outcomes captured for every run

- [x] Best jointly feasible deletion count (primary).
- [x] Normalized area under best-feasible-deletions versus evaluated-states.
- [x] Number and fraction of jointly feasible states.
- [x] FBA growth and fraction of WT growth.
- [x] RBA and FBA modeled/unmodeled deletion coverage.
- [x] Essential, conditional, ambiguous, and unknown deletion counts.
- [x] Complete and broken modules.
- [ ] Action-size distribution, lethal-child rate, recovery count, duplicate
  and rejection rate, empty/invalid model outputs, and terminal branches.
- [ ] Wall time, requests, tokens, account spend, cost-to-best, and deletions per
  dollar.
- [ ] Pairwise deletion-set overlap and between-run diversity.

For valid early method stops, carry the best-so-far value forward when computing
trajectory area. Preserve technical failures, but rerun the exact same arm and
seed from a clean graph after provider outages, authentication failures,
evaluator crashes, corrupted graphs, or indeterminate solver results. Never
replace an unfavorable seed.

## Statistical analysis

- [x] Preserve every first-five run and report median, IQR, range, and mean.
- [x] Estimate first-five paired seed-block differences with 10,000 paired
  bootstrap resamples and 95% confidence intervals. Never bootstrap individual
  DAG states.
- [x] Report the median paired deletion difference and paired probability of
  superiority as effect sizes.
- [x] Use an exact paired sign-flip/permutation test for the predeclared primary
  contrasts, labeling the five-seed result non-confirmatory.
- [x] Holm-correct the primary family: Sol versus random and Qwen versus random.
- [ ] Holm-correct the ablation family: canonical Sol versus scheduler, gate,
  action-size, and evidence-exposure arms.
- [ ] Keep Sol-versus-Qwen and secondary endpoints descriptive unless promoted
  before the runs begin.
- [ ] Report provider/model nondeterminism: the seed fixes our blinded mapping,
  ordering, and search RNG but may not fully determine provider sampling.

Five paired seeds are descriptive. Even perfect directional consistency has a
minimum two-sided exact p-value of 0.0625. Six is the mathematical minimum for
such a test to cross 0.05; ten is the planned minimum for more stable intervals.

## Phase 4: prospective vEcoli validation

The previous WCM experiment is complete and should remain a distinct validation
cohort. For the fresh corrected comparison:

- [ ] Select finalists before reading new vEcoli outcomes.
- [ ] Select at least five independently discovered genotypes per claimed
  method—for example, the deterministic primary winner from the first five
  locked seeds rather than five states cherry-picked from one graph.
- [ ] Run each finalist at three or more lineage seeds for up to 20 generations,
  with matched WT, benign-deletion, essential-deletion, and orchestration
  controls.
- [ ] Treat candidate genotype as the biological/statistical unit and lineage
  seed as a repeated measure. Do not call serial generations independent
  replicates.
- [ ] Analyze survival to generation 20, division time, mass doubling, temporal
  trends, and model/workflow failures separately.
- [ ] Explain or supersede the stale seed-101 intermediate `status.json` that
  shows one lineage at 19/20 while the later final results record 20/20.

## Claim boundary

If the checklist succeeds, the defensible central claim is:

> At a fixed computational budget, agent-guided recoverable search finds more
> extensively reduced genomes satisfying metabolic and resource-allocation
> constraints than random or structural-frontier baselines; scheduler,
> evidence, action-size, and gate ablations explain the improvement, and a
> predeclared subset sustains division in a pinned whole-cell model.

This remains an in-silico genome-design result. Wet-lab construction is required
for a claim of biological viability.
