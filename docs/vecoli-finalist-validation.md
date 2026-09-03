# vEcoli finalist validation

## Decision

The resource-gated Sol search is followed by a predeclared whole-cell-model
validation pass in the official [vEcoli](https://github.com/CovertLab/vEcoli)
codebase. This is an offline finalist test, not another search evaluator. The
search graph and candidate selection are frozen before any reduced-genome
control or vEcoli result is read.

Five candidates are selected from the completed graph:

1. the jointly FBA-positive and RBA-feasible state with the most deletions;
2. four additional jointly feasible states from the band containing at least
   90% as many deletions as the first state, chosen greedily to maximize their
   minimum Jaccard distance from candidates already selected.

Ties are resolved by deletion count, FBA growth, then state ID. Selection uses
only the active evaluator identities stored in the run. MDS42, MS56, EMine, and
all vEcoli outcomes are excluded from selection.

## Simulation contract

- vEcoli source: exact Git commit
  `b2078bd8e226c5d319bb9ddaa10a1f2f1fcfdbbc`, its committed `uv.lock`, and
  the repository-pinned Nextflow 25.10.4 workflow engine.
- Model construction: one shared ParCa run with operons disabled. This makes
  every targeted cistron a separate RNA, matching the original whole-cell
  model's knockout semantics without deleting untargeted operon partners.
- Environment: the vEcoli basal condition (`M9 Glucose minus AAs` in ParCa and
  the matching minimal simulation medium).
- Genotype mapping: canonical MG1655 b-numbers map through the frozen registry's
  exact EcoCyc gene ID. Every requested target must map to exactly one vEcoli
  cistron and one single-cistron RNA before the variant is accepted. Missing
  targets remain explicit and make that candidate's whole-cell result invalid;
  they are never silently treated as safe.
- Knockout: final expression is multiplied by zero for every targeted RNA.
  Variant construction verifies all affected expression and regulatory arrays
  after applying the complete multi-gene deletion set.
- Lineage: one initial seed (`101`), one daughter followed at each division, and
  no more than 20 generations per candidate. Independent candidates may execute
  concurrently, but generations within a lineage remain causally ordered.
- Nondivision: `fail_at_max_duration=true`. A generation counts only when the
  simulation exits successfully and produces the selected daughter state.
  Timeout/nondivision, model exception, resource failure, and orchestration
  failure are recorded separately; only timeout/nondivision is biological
  evidence.
- Output: the exact source graph and registry hashes, selected state IDs and
  deletion-set hashes, b-number-to-vEcoli mapping, workflow configuration,
  variant hashes, vEcoli revision and dependency-lock hash, task trace, logs,
  generation outcomes, elapsed time, and content hashes are retained. Large
  intermediate work directories may be discarded only after the durable
  result bundle has been verified.

## Acceptance checks

1. Finalist selection is deterministic, run-scoped, active-evaluator-scoped,
   and cannot load reduced-genome or vEcoli validation data.
2. A changed graph, state payload, registry, vEcoli revision, lockfile, or
   knockout adapter is rejected by provenance validation.
3. A separate control workflow shows that wild type and a known neutral single
   knockout divide, while a known essential single knockout is rejected or
   fails in generation 1 under the same frozen simData and adapter.
4. Multi-gene construction rejects duplicate, unknown, ambiguous, or
   operon-coupled targets and proves every requested RNA expression parameter
   is zero in the serialized variant.
5. One local one-generation finalist smoke test completes before any scale-out.
6. Each of the five frozen finalists is reported as 0--20 completed
   generations with an explicit terminal reason and no post-hoc candidate
   substitution.
7. If every finalist biologically fails, the search proposal strategy is
   revisited; the failed candidates remain the reported primary result.

## Interpretation limits

vEcoli is a mechanistic whole-cell simulation, not an experimental viability
assay. Disabling operons is a deliberate projection that preserves independent
gene knockout semantics but changes transcriptional organization from the
default model. One stochastic lineage per candidate is a discriminator, not a
robust survival probability; survivors require replicate seeds, and simulated
failures require model-diagnostic review before being called biologically
lethal.
