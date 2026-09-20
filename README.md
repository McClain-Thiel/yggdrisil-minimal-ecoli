# Minimal *E. coli*

A notebook experiment in gene-set minimization for *E. coli* K-12 MG1655,
using [Yggdrisil](https://github.com/McClain-Thiel/yggdrisil) for search and
COBRApy for flux-balance analysis. Start in `scripts/experiment.py`; the
package holds reusable biological data loading, evaluators, and policies.

COBRApy evaluates metabolic growth and gene knockouts. Lark parses KEGG module
definitions and [boolean.py](https://booleanpy.readthedocs.io/en/latest/users_guide.html)
evaluates their Boolean logic. Source preparation uses gffutils, pandas, Pooch,
and pysam; local code supplies the study-specific mappings and evidence reports.

```bash
uv sync --extra notebooks --extra fba --extra dev
uv run marimo edit scripts/experiment.py
```

Choose prepared data, a random or heuristic policy, and a small search budget
in the notebook, then run the experiment. Results are saved as local SQLite
graphs under `runs/`; use a separate graph for each independent experiment.

Model-backed searches are optional: install `--extra agents`, set
`OPENROUTER_API_KEY` in your environment or `~/.env`, and provide a fixed model
ID. The notebook requires a separate action to enable a paid search.

## Layout

```text
src/yggdrisil_ecoli/   Reusable data loading, scoring, search, and analysis
scripts/              Experiments and preparation runs, including marimo notebooks
tests/                Library tests and small fixtures
```

Experiment parameters and plots belong in `scripts/`. Move shared functions into
`src/`; scripts import the package, never the other way around. Use marimo for
interactive experiments and ordinary Python for batch runs.

## Data

Large inputs and generated results belong outside Git. The notebook accepts a
local prepared-data directory or a Hugging Face dataset ID and pinned revision.
No dataset has been published for this prototype yet. Local data are ignored;
small synthetic fixtures remain with the tests.

For one-time source preparation, install `--extra data` and call
`yggdrisil_ecoli.data_build.build_data(Path("data"), accept_kegg_terms=True)`
after reviewing KEGG's terms. Ordinary experiments reuse prepared inputs.

The prepared inputs are a gene registry and essentiality table (Parquet), a
KEGG module catalog (JSON), and the iML1515 model (JSON). Source files, hashes,
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
do not prove a strain is viable. Published reduced-genome labels are used only
for analysis after search.

## Development

```bash
uv sync --python 3.11 --all-extras
uv run pytest
uv run ruff check .
uv run mypy src
```

Yggdrisil's revision and the numerical solver versions are pinned in
`pyproject.toml`. Search graphs record input identities and settings so a
run can be traced to its inputs. Each experiment requires a new graph path.
