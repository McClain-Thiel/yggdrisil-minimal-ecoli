# Baseline and ablation plan

The paper-facing phased execution and statistical checklist is maintained in
[`publication-experiment-checklist.md`](publication-experiment-checklist.md).

This prospective plan applies to runs started after the seed-101 Sol/Qwen
comparison. It does not retroactively preregister earlier exploratory results.
Every arm uses a separate SQLite graph and records the application source hash,
framework revision, evaluator identities, candidate-universe hash, policy
configuration, and seed in run metadata.

## Primary outcome

Select the state with the most deleted eligible genes among states passing the
arm's declared hard gate. Break ties by higher FBA growth and then state ID.
Always report FBA and RBA results for every arm, including `fba-only`; changing
the gate changes parent expansion, not which evaluators are run.

Secondary outcomes are the number of jointly FBA-positive/RBA-feasible states,
FBA growth margin, RBA/FBA modeled-deletion coverage, essentiality categories,
broken modules, action-size distribution, lethal-child rate, recovery count,
duplicate/rejection rate, wall time, model tokens/cost, and deletion-set overlap.

## Baselines

Use seeds 101, 202, 303, 404, and 505 in the 1,216-gene WCM intersection.
First run a 193-state/48-step calibration matching the completed model pair,
then extend informative arms to 500 states.

1. `random-fixed20`: random viable parents and uniformly sampled 20-gene actions.
2. `random-uniform`: random viable parents and a uniform action size from 1 to 20.
3. `heuristic-fixed20`: deepest viable structural-frontier parent and 20-gene actions.
4. `heuristic-uniform`: the same frontier policy with uniform 1-to-20-gene actions.

The heuristic's inability to reopen a viable non-leaf is part of the baseline,
not an implementation failure. Report early termination as an outcome.

## One-factor agent ablations

Use the same model, seed, candidate universe, state/step limits, blinded mapping,
candidate ordering, evaluator suite, and provider limits as the corresponding
closed-book recoverable arm. Change one declared factor at a time:

1. Scheduler: `recoverable` versus `frontier-only`.
2. Hard gate: `fba-rba` versus `fba-only`.
3. Action sizing: model-selected `variable-1-max` versus `fixed-max`.
4. Evidence exposure: `closed-book` versus `tool-rich`.
5. Candidate universe: `wcm-1219` versus the full 4,290-gene registry.

The existing closed-book, recoverable, variable-size, FBA+RBA arm is the
reference and should not be rerun merely to spend equal money. Run ablations in
the order above, with an authenticated aggregate cost guard. Do not pool an arm
that stopped for a provider or budget failure with completed arms.

## Replication and claims

Five seeds per model are the minimum primary comparison. Report every seed and
an aggregate with uncertainty; do not select a favorable seed. WCM-universe
results are controlled action-space experiments, not exact EMine-737
rediscovery. MDS42/MS56 were used to calibrate the RBA threshold and are positive
calibration controls rather than untouched validation. vEcoli/WCM simulation
and experimental construction remain external validation stages.
