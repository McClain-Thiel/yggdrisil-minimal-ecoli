import marimo

__generated_with = "0.24.2"
app = marimo.App(width="medium")


with app.setup:
    import json
    from datetime import datetime
    from importlib.metadata import distribution
    from pathlib import Path

    import marimo as mo
    import yggdrisil as yg
    from huggingface_hub import snapshot_download

    import yggdrisil_ecoli
    from yggdrisil_ecoli.analysis import summarize_run
    from yggdrisil_ecoli.data.evidence import load_genes
    from yggdrisil_ecoli.data.io import file_sha256
    from yggdrisil_ecoli.policies import deletion_sampler
    from yggdrisil_ecoli.problem import EcoliProblem
    from yggdrisil_ecoli.scorers.base import active_evaluator_ids
    from yggdrisil_ecoli.scorers.essentiality import EssentialityScorer
    from yggdrisil_ecoli.scorers.fba import FBAScorer
    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
    from yggdrisil_ecoli.scorers.size import GenomeSizeScorer


@app.cell
def introduction():
    mo.md("""
    # Minimal *E. coli*

    Load the gene table, define four pieces of evidence, choose a policy, and run
    Yggdrisil. The experiment is below; reusable biological calculations live in
    `src/yggdrisil_ecoli`. Edit a parameter or policy directly, then click Run.
    """)
    return


@app.cell
def data_settings():
    # Use local prepared data, or supply an existing Hugging Face dataset and commit.
    local_data = Path("data")
    dataset_id = ""
    data_revision = ""
    load_data = mo.ui.run_button(label="Load prepared data")
    load_data
    return data_revision, dataset_id, load_data, local_data


@app.cell
def load(data_revision, dataset_id, load_data, local_data):
    mo.stop(not load_data.value, mo.md("Load the prepared data to begin."))
    if dataset_id:
        mo.stop(not data_revision, mo.md("Set the dataset's pinned commit first."))
        data_dir = Path(
            snapshot_download(
                repo_id=dataset_id,
                repo_type="dataset",
                revision=data_revision,
                allow_patterns=["processed/*", "external/iML1515.json"],
            )
        )
    else:
        data_dir = local_data
    input_files = {
        "genes": data_dir / "processed/genes.parquet",
        "modules": data_dir / "processed/kegg_modules.json",
        "model": data_dir / "external/iML1515.json",
    }
    input_hashes = {name: file_sha256(path) for name, path in input_files.items()}
    genes = load_genes(input_files["genes"])
    genes.head(10)
    return genes, input_files, input_hashes


@app.cell
def evaluators(genes, input_files, input_hashes):
    module_evaluator = ModuleEvaluator.from_json(input_files["modules"], genes)
    evaluators = [
        GenomeSizeScorer(genes),
        EssentialityScorer(genes=genes, artifact_hash=input_hashes["genes"]),
        module_evaluator,
        FBAScorer(genes=genes, model_path=input_files["model"]),
    ]
    evaluator_ids = active_evaluator_ids(evaluators)
    return evaluator_ids, evaluators


@app.cell
def search_settings():
    seed = 17
    bundle_size = 1
    n_proposals = 2
    max_states = 10
    agent_config = None
    # from yggdrisil_ecoli.agent_policy import AgentSearchConfig
    # Replace None with AgentSearchConfig(
    #     model="vendor/model", seed=seed, bundle_size=bundle_size,
    #     max_actions=n_proposals, mode="closed-book")
    allow_paid = mo.ui.checkbox(label="Enable paid model calls")
    start_search = mo.ui.run_button(label="Run search")
    mo.hstack([allow_paid, start_search], justify="start")
    return (
        agent_config,
        allow_paid,
        bundle_size,
        max_states,
        n_proposals,
        seed,
        start_search,
    )


@app.cell
def provenance(input_hashes, start_search):
    mo.stop(not start_search.value)
    # Record the exact inputs and code alongside every experiment.
    _package = Path(yggdrisil_ecoli.__file__).parent
    _framework = distribution("yggdrisil")
    _install = json.loads(_framework.read_text("direct_url.json") or "{}")
    provenance = {
        "inputs": input_hashes,
        "application": {
            "version": yggdrisil_ecoli.__version__,
            "source_sha256": yg.stable_hash(
                {
                    str(_path.relative_to(_package)): file_sha256(_path)
                    for _path in _package.rglob("*.py")
                }
            ),
            "notebook_sha256": file_sha256(Path(__file__)),
        },
        "framework": {
            "version": _framework.version,
            "revision": _install.get("vcs_info", {}).get("commit_id"),
        },
    }
    return (provenance,)


@app.cell
async def search(
    agent_config,
    allow_paid,
    bundle_size,
    evaluator_ids,
    evaluators,
    genes,
    max_states,
    n_proposals,
    provenance,
    seed,
    start_search,
):
    mo.stop(not start_search.value, mo.md("Choose a policy below, then run."))
    graph_path = (
        Path("runs") / f"experiment-{seed}-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite"
    )
    graph_path.parent.mkdir(exist_ok=True)
    graph_path.touch(exist_ok=False)  # Refuse to reuse another experiment's graph.
    with yg.SQLiteStateGraph(graph_path) as _graph:
        policy = yg.RandomPolicy(
            deletion_sampler(genes, bundle_size=bundle_size),
            seed=seed,
            n_proposals=n_proposals,
        )
        # Replace the policy above with a heuristic or an agent:
        # from yggdrisil_ecoli.policies import make_heuristic_policy
        # policy = make_heuristic_policy(genes=genes, evaluator_ids=evaluator_ids,
        #     seed=seed, bundle_size=bundle_size, n_proposals=n_proposals)
        # from yggdrisil_ecoli.agent_policy import make_agent_policy
        # policy = make_agent_policy(genes=genes, modules=module_evaluator,
        #     config=agent_config, evaluator_ids=evaluator_ids,
        #     evaluations=_graph.evaluations)
        mo.stop(
            isinstance(policy, yg.NavigatorExplorerPolicy) and not allow_paid.value,
            mo.md("Enable paid model calls before running an agent policy."),
        )
        run = await yg.Runner(
            EcoliProblem(genes, max_genes_per_action=bundle_size),
            policy,
            _graph,
            yg.RunLimits(max_states=max_states, max_steps=max_states),
            evaluators=yg.EvaluatorSuite(evaluators, concurrent=True),
            resume=False,
            metadata={
                **provenance,
                "evaluators": evaluator_ids,
                "policy": type(policy).__name__,
                "seed": seed,
                "bundle_size": bundle_size,
                "n_proposals": n_proposals,
                "agent": agent_config.metadata(genes) if agent_config else None,
            },
        ).run()
    mo.md(f"{run.unique_states} states saved to `{graph_path}`.")
    return (graph_path,)


@app.cell
def results(graph_path):
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
