# Yggdrisil Minimal *E. coli*

A separate scientific application for testing gene-set minimization of
*Escherichia coli* K-12 MG1655 on top of
[Yggdrisil](https://github.com/McClain-Thiel/yggdrisil).

The application provides a canonical gene registry,
experimental-essentiality evidence, KEGG module retention, iML1515
flux-balance analysis, an E. coli resource-balance-analysis (RBA) growth gate,
gene-inspection tools, and a persistent Yggdrisil search DAG. It pins the exact
framework commit used by the search so framework and scientific changes can
continue independently.

## Scientific scope

- Reference: chromosome `NC_000913.3`, assembly `GCF_000005845.2` (`ASM584v2`).
- Canonical universe: 4,290 NCBI-annotated protein-coding genes.
- Candidate universe: either all canonical genes or the pinned 1,216-gene
  canonical intersection with the 1,219-gene WCM universe used for EMine-737.
- Canonical identity: MG1655 `b` locus tags; symbols are display metadata.
- Environment: aerobic M9 minimal medium with glucose at 37 °C.
- Outputs: separate evidence results. There is deliberately no combined
  viability score or scalar reward.
- Unknown or absent model coverage stays unknown; it is never treated as
  evidence that a deletion is safe.

## Reproduce the local evidence build

Install the development environment. This creates an ignored local `uv.lock`;
the numerical evaluator stacks are pinned directly in `pyproject.toml`:

```bash
uv sync --extra dev --extra data --extra fba --extra rba
```

Add `--extra agents` when running model-backed searches. OpenRouter credentials
are loaded from `~/.env`; never pass the key as a command-line argument or put
it in this repository:

```bash
uv sync --extra agents --extra dev --extra data --extra fba --extra rba
```

Build the exact iML1515 model, canonical registry, essentiality table, and KEGG
module catalog in dependency order, then acquire and derive the pinned RBA
artifact:

```bash
uv run python scripts/build_data.py --accept-kegg-terms
uv run --extra rba python scripts/build_rba.py
uv run --extra data python scripts/build_wcm_candidate_universe.py
```

The WCM source contains 1,219 genes. Exactly 1,216 map to this application's
protein-coding registry; the three legacy split-gene identifiers outside the
registry and every source/artifact hash remain explicit in the generated JSON.

Run the fixed biological sanity panel:

```bash
uv run python scripts/validate_environment.py
```

Run small, deterministic baseline searches after the data build:

```bash
uv run yggdrisil-ecoli-search \
  --graph runs/random.sqlite \
  --policy random \
  --seed 17 \
  --max-states 10

uv run yggdrisil-ecoli-search \
  --graph runs/heuristic.sqlite \
  --policy heuristic \
  --seed 17 \
  --max-states 10

uv run --extra fba --extra rba yggdrisil-ecoli-search \
  --graph runs/evolutionary.sqlite \
  --policy evolutionary \
  --candidate-universe wcm-1219 \
  --seed 101 \
  --bundle-size 20 \
  --n-proposals 4 \
  --baseline-action-size-mode uniform-1-max \
  --max-states 193 \
  --max-steps 48

uv run --extra fba --extra rba yggdrisil-ecoli-search \
  --graph runs/minesweeper.sqlite \
  --policy minesweeper \
  --candidate-universe wcm-1219 \
  --seed 101 \
  --bundle-size 20 \
  --n-proposals 4 \
  --max-states 193 \
  --max-steps 48
```

Run a bounded model-backed search by naming an exact OpenRouter model. There is
no default model, so an accidental invocation cannot spend credits. This smoke
configuration uses deterministic open-set navigation, two deletion alternatives per
selected parent, sixteen local tool calls, 800 output tokens, and an estimated $0.02
per explorer invocation:

```bash
uv run --extra agents --extra fba --extra rba yggdrisil-ecoli-search \
  --graph runs/agent-closed.sqlite \
  --policy agent \
  --agent-mode closed-book \
  --candidate-universe wcm-1219 \
  --model openai/gpt-4o-mini-2024-07-18 \
  --seed 101 \
  --bundle-size 20 \
  --n-proposals 2 \
  --open-set-width 16 \
  --parents-per-step 4 \
  --max-states 10
```

`closed-book` replaces canonical genes with a seeded opaque identifier map and
exposes only categorical experimental/model coverage. Prompts and tool returns
contain no locus tags, symbols, descriptions, literature, current-vEcoli, or
published whole-cell-model membership. `tool-rich` exposes the existing gene,
essentiality, and KEGG inspection tools. The action limit is a ceiling rather
than a fixed bundle size. Viable parents remain reopenable after lethal children;
the scheduler keeps a diverse active window, rotates candidate pages, rejects
duplicate sibling actions, and supplies progressively smaller 20/10/5/1 fallback
guidance. Use separate graph files for every model, seed, and exposure mode.
The optional WCM comparison universe constrains every policy and the problem
boundary, participates in the run fingerprint, and does not reveal canonical
identifiers to a closed-book model.

Experimental runs can additionally declare `--viability-gate fba-only`,
`--scheduler-mode frontier-only`, `--agent-action-size-mode fixed-max`, or
`--baseline-action-size-mode uniform-1-max`. These are ablation controls and are
persisted in run metadata; defaults retain the FBA+RBA recoverable search. See
[the baseline and ablation plan](docs/experiment-plan.md) before comparing arms.

All policies use the same five-evaluator suite. Yggdrisil evaluates and caches
every state before the next policy call. Inspect a completed or active graph
with `uv run yggdrisil inspect runs/random.sqlite`. Reopening a graph resumes
only when its policy settings, application source/framework revisions, and exact
evaluator/artifact identities match. Use a separate graph for independent
comparisons. `--new-run` creates a new run over the states already present in
that same shared DAG.

Build the agent-invisible reduced-genome controls for calibration or post-run
rediscovery analysis. The NCBI
sequence wrapper must first fetch `NC_000913.3` and MDS42 `AP012306` into the
paths accepted by the builder; MS56 uses Park et al. 2014 Supplementary Table
S3. The builder aligns the two complete chromosomes with minimap2, preserves
the derived reference intervals, intersects both sources with the canonical
search universe, and writes a gitignored artifact:

```bash
uv run --extra data python scripts/build_reduced_genome_validation.py

uv run python scripts/summarize_runs.py \
  --validation data/validation/reduced_genomes.json \
  runs/agent-closed.sqlite
```

The search command never loads this artifact. The summarizer reports overlap,
precision, recall, Jaccard similarity, and MDS42 interval recall only after a
run has finished.

Generated scientific data are gitignored. Each build records source URLs,
versions, access times, content hashes, row counts, mapping gaps, and output
hashes in local manifests under `data/processed/`. KEGG snapshots require the
explicit academic-use acknowledgement and are not redistributed.

## What is implemented

- Strict GFF3 parsing and an immutable, typed Parquet gene registry.
- Audited, left-joined KEGG/KO and iML1515 crosswalks.
- One-row-per-gene Choe 2023 evidence with condition-aware summary classes and
  explicit unknown coverage.
- A KEGG module grammar supporting AND, OR, optional terms, and module
  references, with exact minimal missing-KO sets.
- Copy-on-score COBRApy gene deletion with explicit medium, objective, ATPM,
  solver, provenance, coverage, and GPR diagnostics.
- Fixed-growth RBA feasibility with protein, enzyme, translation, chaperone,
  secretion, compartment, and proteome-allocation constraints; modeled and
  unmodeled deletions remain explicit. The pinned SciPy HiGHS solver uses a
  recorded interior-point/no-presolve fallback only for indeterminate numerical
  statuses; its ordered methods and RBAtools matrix backend participate in
  evaluator identity.
- `GenomeState`, `DeleteGenes`, and thin monotonic problem semantics,
  registered for safe Yggdrisil persistence.
- Independent size, essentiality, module-retention, FBA, and RBA evaluators using
  Yggdrisil's native evaluator contract. A small shared helper keeps scalar
  metrics separate from structured details, coverage, and provenance.
- Concurrent search-time evaluation and SQLite DAG caching keyed by state,
  scorer version, and source/configuration fingerprints.
- Fixed-seed `RandomPolicy` and a deliberately small heuristic baseline that
  expands parents that are FBA-positive and RBA-feasible at 0.1 h^-1 without
  treating experimental essentiality as a deletion ban.
- Two stronger no-LLM comparators: steady-state constrained evolution with
  union crossover, and a clean-room, 20-gene-capped Minesweeper-style segment,
  combination, and lethal-bundle-bisection policy. Both reconstruct all search
  state from the persisted DAG for exact resume behavior.
- A bounded OpenRouter explorer with deterministic recoverable open-set scheduling,
  fixed model identity, prompt/config provenance, per-call usage limits, and
  closed-book versus tool-rich evidence modes.
- An agent-invisible MDS42/MS56 calibration and rediscovery evaluator built from
  a complete-genome alignment and the original published MS56 deletion table.
  These controls were used to calibrate the RBA growth floor and are therefore
  not an untouched evaluator-level validation set.
- Canonical-only gene inspection, set analysis, and frozen-module inspection
  tools.

The latest fully populated local build produced 4,290 canonical genes, 4,288
KEGG gene mappings, 3,244 genes with KO mappings, 1,513 genes mapped into
iML1515, and 112 wild-type-complete KEGG modules. These are observed snapshot
counts, not hard-coded biological assumptions; rebuilding generates a fresh
audit.

The RBA evaluator maps 1,441 canonical genes to enzyme or process-machine
variables. It predicts balanced growth rather than complete cellular viability,
so candidates still require an untouched comparison and selective vEcoli/WCM
simulation or experiments.

## Development gates

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src scripts
uv build
```

See [the data contract](docs/data-contract.md), [source ledger](docs/sources.md),
and [framework handoff](docs/framework-handoff.md) for the pinned integration
boundary and remaining agent work.
