import marimo

__generated_with = "0.24.2"
app = marimo.App(width="medium")


@app.cell
def imports():
    from datetime import datetime
    from pathlib import Path

    import marimo as mo
    from huggingface_hub import snapshot_download

    from yggdrisil_ecoli.agent_policy import AgentSearchConfig
    from yggdrisil_ecoli.analysis import summarize_run
    from yggdrisil_ecoli.search import SearchArtifacts, run_search

    return (
        AgentSearchConfig,
        Path,
        SearchArtifacts,
        datetime,
        mo,
        run_search,
        snapshot_download,
        summarize_run,
    )


@app.cell
def introduction(mo):
    mo.md("""
    # Minimal *E. coli*

    Load prepared evidence, choose a policy, and inspect a small experiment.
    Yggdrisil runs and records the search; the package supplies reusable biology.
    Edit the parameters below, then use the load and run buttons.
    """)
    return


@app.cell
def data_settings(Path, mo):
    # Use local prepared data, or supply an existing Hugging Face dataset and commit.
    local_data = Path("data")
    dataset_id = ""
    data_revision = ""
    load_data = mo.ui.run_button(label="Load prepared data")
    load_data
    return data_revision, dataset_id, load_data, local_data


@app.cell
def load(
    Path,
    SearchArtifacts,
    data_revision,
    dataset_id,
    load_data,
    local_data,
    mo,
    snapshot_download,
):
    mo.stop(not load_data.value, mo.md("Load the prepared data to begin."))
    if dataset_id:
        mo.stop(not data_revision, mo.md("Set the dataset's pinned commit first."))
        _path = Path(
            snapshot_download(
                repo_id=dataset_id,
                repo_type="dataset",
                revision=data_revision,
                allow_patterns=["processed/*", "external/iML1515.json"],
            )
        )
    else:
        _path = local_data
    artifacts = SearchArtifacts(_path)
    mo.md(f"Prepared data: `{artifacts.data_dir}`")
    return (artifacts,)


@app.cell
def search_settings(mo):
    # Policies: "random", "heuristic", or "agent". Each run gets a new graph.
    policy_name = "random"
    seed = 17
    max_states = 10
    model = ""  # A fixed OpenRouter model ID is required for the agent policy.
    mode = "closed-book"  # Or "tool-rich" for gene and module inspection tools.
    allow_paid = mo.ui.checkbox(label="Enable paid model calls")
    start_search = mo.ui.run_button(label="Run search")
    mo.hstack([allow_paid, start_search], justify="start")
    return allow_paid, max_states, mode, model, policy_name, seed, start_search


@app.cell
async def search(
    AgentSearchConfig,
    Path,
    allow_paid,
    artifacts,
    datetime,
    max_states,
    mo,
    mode,
    model,
    policy_name,
    run_search,
    seed,
    start_search,
):
    mo.stop(not start_search.value, mo.md("Choose a policy, then run the search."))
    _agent = None
    if policy_name == "agent":
        mo.stop(
            not allow_paid.value or not model,
            mo.md("Set a fixed model ID and enable paid model calls before running."),
        )
        _agent = AgentSearchConfig(model=model, mode=mode, seed=seed)

    graph_path = (
        Path("runs") / f"{policy_name}-{seed}-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite"
    )
    graph_path.parent.mkdir(exist_ok=True)
    await run_search(
        artifacts=artifacts,
        graph_path=graph_path,
        policy_name=policy_name,
        seed=seed,
        max_states=max_states,
        max_steps=max_states,
        resume=False,
        agent_config=_agent,
    )
    mo.md(f"Search saved to `{graph_path}`.")
    return (graph_path,)


@app.cell
def results(graph_path, mo, summarize_run):
    summary = summarize_run(graph_path)
    _candidate = summary["deepest_viable_candidate"]
    mo.stop(_candidate is None, mo.md("No candidate passed the evidence filters."))
    _evidence = _candidate["evaluations"]
    mo.vstack(
        [
            mo.md("""## Results

    Largest deletion set with positive predicted growth and no known essential
    deletions. Missing evidence stays unknown; strain viability is unproven.
    """),
            mo.ui.table(
                [
                    {
                        "Genes deleted": _candidate["genes_deleted"],
                        "Predicted growth (1/h)": _candidate["growth_rate"],
                        "Modules retained": _evidence["module_retention"]["n_complete"],
                        "Essential genes deleted": _evidence["essentiality"][
                            "n_essential_deleted"
                        ],
                        "Unmodeled deletions": _candidate["coverage"]["fba"][
                            "deleted_genes_unmodeled"
                        ],
                    }
                ]
            ),
            mo.md("Deleted genes: " + ", ".join(_candidate["deleted_gene_ids"])),
        ]
    )
    return


if __name__ == "__main__":
    app.run()
