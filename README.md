# Minimal *E. coli*

A notebook experiment in gene-set minimization for *E. coli* K-12 MG1655,
using [Yggdrisil](https://github.com/McClain-Thiel/yggdrisil) for search and
COBRApy for flux-balance analysis and RBApy/RBAtools for resource allocation.
Start in `scripts/experiment.py`; the
package holds reusable biological data loading, evaluators, and policies.

COBRApy evaluates metabolic growth and gene knockouts. Lark parses KEGG module
definitions and [boolean.py](https://booleanpy.readthedocs.io/en/latest/users_guide.html)
evaluates their Boolean logic. Source preparation uses gffutils, pandas, Pooch,
and pysam; local code supplies the study-specific mappings and evidence reports.

```bash
uv sync --extra notebooks --extra fba --extra rba --extra dev
uv run marimo edit scripts/experiment.py
```

The notebook shows the complete flow: load data, construct evaluators, choose a
policy, run Yggdrisil, and inspect results. The policy selector offers random and
heuristic baselines, persistent manager search, literature research, and the
original closed-book experiment. New
experiments save a fresh SQLite graph under `runs/`. To continue the same
experiment, enter its graph path and increase the state limit; changed inputs,
code, evaluator identities, or policy settings are rejected before resume.

Agent searches keep viable parents available after failed deletions and use
smaller bundles as fallback guidance. All policies require positive FBA growth
and RBA feasibility at the fixed 0.1 h⁻¹ growth floor. Essentiality and KEGG
modules remain separate ranking evidence.

Model-backed searches are optional: install `--extra agents`, set
`OPENROUTER_API_KEY` in your environment, repo `.env`, or `~/.env`, and provide a
fixed model ID. The notebook requires a separate action to enable a paid search.

Choose `research` in the notebook for persistent manager/worker search with
ordinary literature retrieval; `manager` uses only local evidence.
Every run starts at full MG1655. Google ADK owns the persistent plan, shared
research documents, delegation and event history in `runs/agents.sqlite` (set
`session_path` to change it). The search database owns genome states, proposals
and evaluator results. Run IDs isolate agent memory. Keep both databases when
resuming or sharing a run. The closed-book/recoverable policy remains available;
ADK runs use a new prompt/runtime version and should be compared as a new condition.

The navigator calls the native ADK `research_literature(request)` subagent as a
tool. It retrieves sources, resolves IDs and saves cited documents in shared
state, immediately available to genome explorers. Only explorers propose
changes to genomes. Research is limited to one invocation per navigator turn.

For Edison, set `EDISON_PLATFORM_API_KEY` in `.env` and enable the notebook's
Edison checkbox. The research specialist can submit one new Literature question
per experiment. Submission returns a job ID immediately; Edison takes about
13–20 minutes. The coordinator checks jobs between exploration steps without
model calls, then exposes finished reports in read-only job state.
The research tool resolves reports into actionable targets when asked by the navigator.
Jobs and cited answers are cached in `runs/edison.sqlite`; exact repeated
questions reuse the same job, including after restart. Cache entries and the
query limit are isolated by run ID. If exploration reaches its budget first,
resume the same graph later to collect the result. The remote job continues,
but the local coordinator does not keep running after the experiment stops.
Later questions use ordinary search and shared documents. Edison costs are
separate from model-call budgets. Retrieval failures raise explicitly.
Gene tools resolve annotated identifiers without guessing ambiguous matches and
compare cited deletion sets against graph states. Saved deletion-set metadata
must include `reference="NC_000913.3"`, canonical `gene_ids`, and `source_url`.
Published noncoding changes or compensatory mutations remain qualifications:
the executable state represents protein-coding gene deletions only.

A navigator and its research subagent share one invocation budget; each genome
explorer has its own budget. Enable
paid calls only after choosing a fixed model and a state/step budget. The offline
managed-search tests exercise restart and target reconstruction without model
or literature requests.

For local framework development, install the edited core into this environment
with `uv pip install "/path/to/yggdrisil[agents]"` after syncing dependencies.
The Git dependency pin must be updated to the ADK framework revision before
these application changes can be installed directly from GitHub.

## Recorded experiments

The [experiment ledger](experiments/README.md) links the verified agent and
non-LLM baseline results to their exact source, frozen configurations, S3 data,
W&B artifact versions, and independent audits. The [historical methods
snapshots](methods/20261106/README.md) preserve the code actually used, including
the framework revision and validation overlays. These studies used a common
1,030-deletion starting genome and recorded checkpoint continuations; they do
not use the default full-MG1655 start described above.

Genomic searches, simulations, and their controllers run only on remote Linux.
Local inspection, editing, and lightweight tests are allowed. Full scientific
data stays in private S3; Git contains source and compact ledger records.

## Layout

```text
src/yggdrisil_ecoli/   Reusable data loading, evaluators, policies, and analysis
scripts/experiment.py Search experiment (marimo)
scripts/prepare_data.py Source preparation recipe (marimo)
tests/                Library tests and small fixtures
```

Experiment parameters and plots belong in `scripts/`. Move shared functions into
`src/`; scripts import the package, never the other way around. Use marimo for
interactive experiments and ordinary Python for batch runs.

## Data

Large inputs and generated results belong outside Git. Point the notebook at a
local prepared-data directory. No dataset has been published for this prototype
yet. Local data are ignored;
small synthetic fixtures remain with the tests.

For one-time source preparation:

```bash
uv sync --extra notebooks --extra data --extra fba --extra rba
uv run marimo edit scripts/prepare_data.py
```

Review KEGG's terms before enabling its downloads. Ordinary experiments reuse
prepared inputs. Use the notebook's separate RBA preparation button to build
the pinned model with your installed numerical libraries; incompatible artifact
versions are rejected by the evaluator.

The prepared inputs are one indexed gene-evidence table (`genes.parquet`), a
KEGG module catalog (JSON), the iML1515 model (JSON), and a local RBA artifact.
Pandas joins the gene
annotations, crosswalks, and essentiality measurements; validators check the
scientific input boundaries. Source files, hashes,
and preparation details travel with the dataset, rather than being repeated in
repository documentation. KEGG-derived material needs a redistribution check
before publication; it can remain a local input.

Sources: [NCBI MG1655](https://www.ncbi.nlm.nih.gov/nuccore/NC_000913.3),
[Choe 2023 essentiality](https://doi.org/10.1128/msystems.00896-22),
[KEGG](https://www.kegg.jp/kegg/),
[iML1515](https://doi.org/10.1038/nbt.3956), and held-out
[MDS42](https://www.ncbi.nlm.nih.gov/nuccore/AP012306) /
[MS56](https://doi.org/10.1007/s00253-014-5739-y) deletions.
Consult the dependencies' licenses and each data provider's terms, including
[NCBI policies](https://www.ncbi.nlm.nih.gov/home/about/policies/).

## Interpretation

The search deletes protein-coding genes identified by MG1655 `b` locus tags
(reference `NC_000913.3`, assembly `GCF_000005845.2`). It reports genome size,
essentiality, KEGG module retention, and predicted growth separately for
aerobic M9 with glucose at 37 °C. Missing evidence stays unknown; these scores
do not prove a strain is viable. KEGG scoring reports complete and broken
modules, without enumerating possible repairs. The RBA model covers 1,441 of
4,290 genes; uncovered deletions remain explicit. RBA uses its published medium
and exact enzyme/process-machine knockouts at the fixed growth floor.

MDS42 and MS56 are agent-invisible calibration controls, used to check the RBA
floor before search. Their later overlap scores measure rediscovery, not
independent viability validation. `scripts/calibrate_resource_gate.py` reproduces
those checks against a frozen prior FBA-only candidate.

## vEcoli finalists

After search, `scripts/prepare_vecoli_finalists.py` freezes five diverse finalists
that pass both growth gates and maps the same `genes.parquet` to exact vEcoli IDs.
Run it with `--help` for the graph, pinned checkout, and output paths. It prepares
a workflow; it does not launch simulations.

Run the generated configuration from the pinned vEcoli checkout using its
`runscripts/workflow.py`, then use `scripts/summarize_vecoli_finalists.py` to report
completed divisions and distinguish nondivision from model or execution failures.
Start with one generation before a longer lineage. Selection uses only the
frozen search evidence: the largest feasible deletion set, then four diverse
sets within 90% of its size. The pinned vEcoli workflow disables operons to
knock out individual genes, follows one daughter for up to 20 generations,
and checks every targeted expression/regulation parameter. A single simulated
lineage is not a survival probability; repeat seeds and experimental validation
are needed for biological conclusions.

## Development

```bash
uv sync --python 3.11 --all-extras
uv run pytest
uv run ruff check .
uv run mypy src
```

Yggdrisil's revision and the numerical solver versions are pinned in
`pyproject.toml`. Search graphs record input identities and settings so a
run can be traced to its inputs. Independent experiments use separate graph paths.
