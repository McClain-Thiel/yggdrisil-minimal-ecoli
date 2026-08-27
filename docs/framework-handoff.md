# Yggdrisil framework handoff

The application is pinned to Yggdrisil commit
`d114144e4696989aacb85a4159c4177909deef1f`. Runner-owned evaluator scheduling,
resume backfill, and opt-in concurrent suites landed through
[Yggdrisil PR #1](https://github.com/McClain-Thiel/yggdrisil/pull/1). The pinned
commit additionally exposes canonical evaluator identities through draft
[Yggdrisil PR #2](https://github.com/McClain-Thiel/yggdrisil/pull/2), allowing
policies to select exactly the evidence from their active suite. No biological
concept was added to the framework.

## Stable application boundary

- `GenomeState` contains only the immutable set of deleted canonical genes and
  is registered with Yggdrisil's explicit serializer.
- `DeleteGenes` is a sorted, non-empty, validated and registered deletion
  bundle.
- `EcoliProblem` owns direct-child validation, monotonic application, stable
  state keys, and an explicit problem fingerprint.
- Scientific evaluators implement Yggdrisil's `Evaluator` protocol directly.
  Scalars remain metrics; a small application helper moves lists and nested
  structures to `details`, while coverage and provenance retain their own
  metadata fields.
- The framework `EvaluatorSuite(concurrent=True)` and `Runner(evaluators=...)`
  own search-time scheduling and persistent cache records. The application
  never defines a combined reward.
- `RandomPolicy` and `SimpleHeuristicPolicy` run against the same problem and
  evaluations. Their decisions and proposals are persisted by the framework.
- The search entrypoint refuses an accidental resume when policy identity,
  seed, bundle size, proposal count, or application differs.
  `--new-run` still shares existing DAG states; independent experiments use
  separate graph files.

The VCS dependency uses an exact full commit. Update it deliberately when
adopting a later framework revision, then run the integration and resume tests;
the local `uv.lock` is generated and intentionally ignored.

## Integration details to recheck

Core `Problem` requires `initial_state`, `state_key`, and `apply`, with optional
validation hooks. `EcoliProblem` already satisfies that boundary directly.

Each newly inserted or restored state receives `genome_size`, `essentiality`,
`module_retention`, and `fba` evaluations before policy execution. Changing a
scorer version or configuration hash creates a new evaluation identity and is
backfilled on resume. A scorer failure leaves valid transition provenance
intact and is retried through evaluation backfill.

Explorer traces belong in Yggdrisil decisions, not inside `GenomeState`, or the
same deletion set would acquire multiple identities. The current
`ExplorerResult` field is named `actions`; integration code must not assume a
`proposals` field from the design note. The future agent prompt should expose
compact canonical gene evidence and require structured `DeleteGenes` output.

## Deferred work

- Add the compact Navigator/Explorer context and closed-book tool configuration,
  then exercise `NavigatorExplorerPolicy` with fake agents before a live model.
- Add published whole-cell-model and current-vEcoli crosswalk discovery.
- Add published reduced-genome baselines and interval-to-registry derivation.
- Add expensive whole-cell simulation only after cached cheap evidence gates
  are stable.
