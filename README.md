# Yggdrisil Minimal *E. coli*

A separate scientific application for testing gene-set minimization of
*Escherichia coli* K-12 MG1655 on top of
[Yggdrisil](https://github.com/McClain-Thiel/yggdrisil).

The application provides a canonical gene registry,
experimental-essentiality evidence, KEGG module retention, iML1515
flux-balance analysis, gene-inspection tools, and a persistent Yggdrisil search
DAG. It pins the exact framework commit used by the search so framework and
scientific changes can continue independently.

## Scientific scope

- Reference: chromosome `NC_000913.3`, assembly `GCF_000005845.2` (`ASM584v2`).
- Search universe: NCBI-annotated protein-coding genes only.
- Canonical identity: MG1655 `b` locus tags; symbols are display metadata.
- Environment: aerobic M9 minimal medium with glucose at 37 °C.
- Outputs: separate evidence results. There is deliberately no combined
  viability score or scalar reward.
- Unknown or absent model coverage stays unknown; it is never treated as
  evidence that a deletion is safe.

## Reproduce the local evidence build

Install the development environment. This creates an ignored local `uv.lock`;
the numerical FBA stack is pinned directly in `pyproject.toml`:

```bash
uv sync --extra dev --extra data --extra fba
```

Add `--extra agents` when running model-backed searches. OpenRouter credentials
are loaded from `~/.env`; never pass the key as a command-line argument or put
it in this repository:

```bash
uv sync --extra agents --extra dev --extra data --extra fba
```

Build the exact iML1515 model, canonical registry, essentiality table, and KEGG
module catalog in dependency order:

```bash
uv run python scripts/build_data.py --accept-kegg-terms
```

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
```

Run a bounded model-backed search by naming an exact OpenRouter model. There is
no default model, so an accidental invocation cannot spend credits. This smoke
configuration permits at most one navigator request, sixteen local tool calls, 800
output tokens, and an estimated $0.02 per navigator or explorer invocation:

```bash
uv run --extra agents --extra fba yggdrisil-ecoli-search \
  --graph runs/agent-closed.sqlite \
  --policy agent \
  --agent-mode closed-book \
  --model openai/gpt-4o-mini-2024-07-18 \
  --seed 101 \
  --bundle-size 1 \
  --n-proposals 1 \
  --max-states 10
```

`closed-book` replaces canonical genes with a seeded opaque identifier map and
exposes only categorical experimental/model coverage. Prompts and tool returns
contain no locus tags, symbols, descriptions, literature, current-vEcoli, or
published whole-cell-model membership. `tool-rich` exposes the existing gene,
essentiality, and KEGG inspection tools. Use separate graph files for every
model, seed, and exposure mode.

Both policies use the same four-evaluator suite. Yggdrisil evaluates and caches
every state before the next policy call. Inspect a completed or active graph
with `uv run yggdrisil inspect runs/random.sqlite`. Reopening a graph resumes
only when its policy settings, application source/framework revisions, and exact
evaluator/artifact identities match. Use a separate graph for independent
comparisons. `--new-run` creates a new run over the states already present in
that same shared DAG.

Build the held-out reduced-genome labels only for post-hoc analysis. The NCBI
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
- `GenomeState`, `DeleteGenes`, and thin monotonic problem semantics,
  registered for safe Yggdrisil persistence.
- Independent size, essentiality, module-retention, and FBA evaluators using
  Yggdrisil's native evaluator contract. A small shared helper keeps scalar
  metrics separate from structured details, coverage, and provenance.
- Concurrent search-time evaluation and SQLite DAG caching keyed by state,
  scorer version, and source/configuration fingerprints.
- Fixed-seed `RandomPolicy` and a deliberately small heuristic baseline that
  avoids known essential genes and FBA-infeasible parent states.
- A bounded OpenRouter navigator/explorer policy with fixed model identity,
  prompt/config provenance, per-call usage limits, and closed-book versus
  tool-rich evidence modes.
- A post-hoc, agent-invisible MDS42/MS56 rediscovery evaluator built from a
  complete-genome alignment and the original published MS56 deletion table.
- Canonical-only gene inspection, set analysis, and frozen-module inspection
  tools.

The latest fully populated local build produced 4,290 canonical genes, 4,288
KEGG gene mappings, 3,244 genes with KO mappings, 1,513 genes mapped into
iML1515, and 112 wild-type-complete KEGG modules. These are observed snapshot
counts, not hard-coded biological assumptions; rebuilding generates a fresh
audit.

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
