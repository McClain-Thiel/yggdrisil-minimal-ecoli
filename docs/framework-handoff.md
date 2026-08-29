# Yggdrisil framework handoff

The application is pinned to Yggdrisil commit
`48cd2b70c96306d0b475d26a8900425a67bfaf58`. Runner-owned evaluator scheduling,
resume backfill, and opt-in concurrent suites landed through
[Yggdrisil PR #1](https://github.com/McClain-Thiel/yggdrisil/pull/1). Canonical
evaluator identities landed through
[Yggdrisil PR #2](https://github.com/McClain-Thiel/yggdrisil/pull/2), allowing
policies to select exactly the evidence from their active suite. No biological
concept was added to the framework.
Agent request, token, cache, tool-call, and cost accounting is persisted through
[Yggdrisil PR #3](https://github.com/McClain-Thiel/yggdrisil/pull/3), which also
adds a generic best-first eligibility predicate so the application does not own
a policy implementation.
[Yggdrisil PR #4](https://github.com/McClain-Thiel/yggdrisil/pull/4) adds
run-scoped deterministic exploration selection, non-leaf reopening, retryable
empty decisions, opt-in partial explorer-failure tolerance, and durable
failure/timeout provenance. It also preserves manual run-viewer pan and zoom
during live polling and fits wide search graphs without cropping. The application
owns the biological open-set ranking and recovery guidance; the framework remains
domain-independent.

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
- Framework-native `RandomPolicy` and `BestFirstPolicy` run against the same
  problem and evaluations using two small application callbacks. Their
  decisions and proposals are persisted by the framework.
- The search entrypoint refuses an accidental resume when policy settings,
  the application source hash/search contract, installed framework revision,
  or any active evaluator identity differs. Evaluator identities include
  artifact and configuration hashes.
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

- Add published whole-cell-model and current-vEcoli crosswalk discovery.
- Add expensive whole-cell simulation only after cached cheap evidence gates
  are stable.
