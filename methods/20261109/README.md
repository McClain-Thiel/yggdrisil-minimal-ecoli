# Search replication methods — 9 November 2026

[Results and storage ledger](../../experiments/records/search-replication-20261109.json) · [Compact comparison](../../experiments/comparisons/search-replication-20261109.json) · [Frozen protocol](protocol.json) · [File checksums](manifest.json)

The experiment assigned five independent search seeds (103, 227, 349, 461, 587) to each of no retrieval, frozen dossier, adaptive literature, greedy block, and randomized block. All started at the same verified 1,030-deletion root, with the same FBA/RBA gates and a 100-candidate ceiling. The agent arms had individually bounded model budgets; every provider failure and budget stop remains in the assigned-run denominator. The frozen protocol specifies proposal, evaluator, information-cutoff, cost, selection, and validation rules.

The collector froze one feasible nominee per terminal run before validation, without replacement after whole-cell outcomes. Ten baseline nominations collapsed to five identical paired genotypes. Validation covered 20 unique candidates plus MG1655 and the common starting genome, each at seeds 101, 211, and 307, one followed daughter, aerobic M9 glucose at 37 C, at most 20 generations and 10,800 simulated seconds per cell. All three workers passed a technical two-generation calibration before dispatch. Technical calibration cells are excluded from the 66-lineage result.

The native checkout is pinned to `b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc`. The [existing strict numerical patch](../20261106/baseline-validation/solver-recovery-v2.patch) was applied uniformly before validation: unchanged LP, objective, constraints and tolerances; GLP_OPT and finite KKT relative residuals at most 1e-7; failed validation latched against native outer retries. No completed cell in this replication needed the fallback. Exceptions are retained, with no outcome-driven scientific repairs.

## Executed code and reproducibility

- [Planner](runtime/scripts/plan_search_replication.py), [search runner](runtime/scripts/run_search_replication.py), [collector and endpoint rule](runtime/scripts/collect_search_replication.py), and [search supervisor](runtime/scripts/supervise_search_replication.py).
- [Baseline policies](runtime/src/yggdrisil_ecoli/policies.py), [agent configuration](runtime/src/yggdrisil_ecoli/agent_policy.py), [archived literature retrieval](runtime/src/yggdrisil_ecoli/tools/literature.py), and [backup implementation](runtime/src/yggdrisil_ecoli/baseline_storage.py).
- [Validation preparation](runtime/scripts/prepare_replication_validation.py), [tracking/controller](runtime/scripts/track_vecoli_experiment.py), [parallel coordinator](runtime/scripts/parallel_replication_validation.py), and [worker supervisor](runtime/scripts/run_replication_validation_shard.py).
- [Complete runtime identity manifest](runtime-manifest.json), including framework file identities and operational revisions. The active validation controller is the `validation/application/` entry. Historical setup/transport failures remain in the ledger; they are not candidate outcomes.
- [Independent archive verifier](analysis/audit-final-stream.py), [failure-evidence audit](analysis/audit-failure-evidence.py), and [baseline proposal audit](analysis/audit-baseline-policy.py).

The `runtime/` files preserve exact executed bytes. Maintained entry points under `scripts/` may receive later operational or formatting changes; their existence does not change this snapshot. Source is versioned in Git, never uploaded as scientific data archives. Credentials are excluded from both public source and scientific backups.

The summary uses the frozen 4,290-gene registry, 1,915-gene current functional panel, and 1,197-gene shared functional panel. These are different denominators. They describe expression knockouts and selected model coverage, not physical DNA shortening or experimental viability.

[The proposed baseline continuation](baseline-depth-extension-draft.json) is an exploratory follow-up designed after seeing these results. It preserves the original equal-ceiling comparison. The separately authorized follow-up is now completed; see the [11 November methods and amendments](../20261111/README.md). No searches or simulations should be launched locally, including these historical source snapshots.
