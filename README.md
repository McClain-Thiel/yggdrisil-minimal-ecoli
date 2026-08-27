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

Install the locked development environment:

```bash
uv sync --extra dev --extra data --extra fba
```

Fetch the exact iML1515 model from the Monk et al. publication supplement, then
build the NCBI registry with optional KEGG and model crosswalks:

```bash
uv run python scripts/fetch_iml1515.py
uv run python scripts/build_gene_registry.py \
  --include-kegg \
  --accept-kegg-terms \
  --iml1515-json data/external/iML1515.json
```

Build the Choe et al. experimental-essentiality tables and KEGG module catalog:

```bash
uv run python scripts/build_essentiality_data.py
uv run python scripts/build_kegg_modules.py --accept-kegg-terms
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

Both policies use the same four-evaluator suite. Yggdrisil evaluates and caches
every state before the next policy call. Inspect a completed or active graph
with `uv run yggdrisil inspect runs/random.sqlite`. Reopening a graph resumes
its latest run only when the policy, seed, bundle size, and proposal count
match. Use a separate graph for independent comparisons. `--new-run` creates a
new run over the states already present in that same shared DAG.

Generated scientific data are gitignored. Each build records source URLs,
versions, access times, content hashes, row counts, mapping gaps, and output
hashes in local manifests under `data/processed/`. KEGG snapshots require the
explicit academic-use acknowledgement and are not redistributed.

## What is implemented

- Strict GFF3 parsing and an immutable, typed Parquet gene registry.
- Audited, left-joined KEGG/KO and iML1515 crosswalks.
- Frozen Choe 2023 observations with condition-aware summary classes.
- A KEGG module grammar supporting AND, OR, optional terms, and module
  references, with exact minimal missing-KO sets.
- Copy-on-score COBRApy gene deletion with explicit medium, objective, ATPM,
  solver, provenance, coverage, and GPR diagnostics.
- `GenomeState`, `DeleteGenes`, and thin monotonic problem semantics,
  registered for safe Yggdrisil persistence.
- Independent size, essentiality, module-retention, and FBA scorers.
- A Yggdrisil evaluator adapter that keeps scalar metrics separate from
  structured details, coverage, and provenance.
- Concurrent search-time evaluation and SQLite DAG caching keyed by state,
  scorer version, and source/configuration fingerprints.
- Fixed-seed `RandomPolicy` and a deliberately small heuristic baseline that
  avoids known essential genes and FBA-infeasible parent states.
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
