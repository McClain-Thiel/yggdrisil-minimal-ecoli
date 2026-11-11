"""Offline checks for treatment isolation, persisted inputs and bounded execution."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from yggdrisil import RunLimits
from yggdrisil.limits import RunStatus

from scripts.run_literature_ablation import TrackedPolicy, check_key_budget, seed_memory
from yggdrisil_ecoli.research import memory_tools
from yggdrisil_ecoli.tools.genes import GeneTools


def test_mac_launcher_stops_before_loading_credentials(monkeypatch, capsys):
    import scripts.run_literature_ablation as runner

    monkeypatch.setattr(runner.sys, "platform", "darwin")
    monkeypatch.setattr(
        runner.sys,
        "argv",
        [
            "runner",
            "--bundle",
            "missing",
            "--job-index",
            "0",
            "--model-budget-usd",
            "8",
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        runner.main()
    assert stopped.value.code == 2
    assert "remote-only" in capsys.readouterr().err


@pytest.mark.parametrize(
    "dossier",
    [None, {"content": "Cited evidence", "source_url": "https://example.org/paper"}],
)
async def test_initial_dossier_survives_restart_and_reaches_explorer(tmp_path, dossier):
    path = tmp_path / "agents.sqlite"
    await seed_memory(path, "run-one", dossier)
    await seed_memory(path, "run-one", dossier)
    await seed_memory(path, "run-two", None)
    sessions = SqliteSessionService(str(path))
    try:
        worker = await sessions.create_session(
            app_name="yggdrisil", user_id="run-one", session_id="explorer-root"
        )
        assert worker.state["user:initial_dossier"] == dossier
        other = await sessions.get_session(
            app_name="yggdrisil", user_id="run-two", session_id="navigator"
        )
        assert other.state.get("user:initial_dossier") is None
    finally:
        await sessions.close()
    with pytest.raises(ValueError, match="differs"):
        await seed_memory(path, "run-one", {"content": "Changed evidence"})


def test_frozen_dossier_can_be_read_but_not_overwritten(genes):
    from yggdrisil.agents import ToolInputError

    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator

    evidence = GeneTools(
        genes,
        ModuleEvaluator(
            registry=genes,
            entries={},
            wt_complete_module_ids=(),
            parser_semantics_version="fixture",
        ),
    )
    tools = {
        tool.__name__: tool for tool in memory_tools(evidence, None, writable=True)
    }
    context = SimpleNamespace(
        state={
            "user:initial_dossier": {
                "content": "Raw report",
                "source_url": "https://example.org/report",
            }
        }
    )
    assert tools["list_shared_memory"](context) == ["literature/initial-dossier"]
    assert (
        tools["read_shared_memory"]("literature/initial-dossier", context)["content"]
        == "Raw report"
    )
    assert (
        tools["read_shared_memory"]("literature/initial-dossier", context)["metadata"][
            "kind"
        ]
        == "literature_note"
    )
    with pytest.raises(ToolInputError, match="resolve_gene_ids"):
        tools["compare_saved_gene_set"]("literature/initial-dossier", "root", context)
    with pytest.raises(ToolInputError, match="immutable"):
        tools["save_shared_memory"](
            "literature/initial-dossier", "Overwrite", {}, context
        )


@pytest.mark.parametrize(
    "usage",
    [
        {"limit": None, "usage": 0},
        {"limit": 6, "usage": 0},
        {"limit": 5, "usage": 0, "limit_reset": "daily"},
        {"limit": 5, "usage": "NaN"},
        {"limit": 5, "usage": 6},
    ],
)
def test_requires_dedicated_provider_cap(usage):
    with pytest.raises(ValueError):
        check_key_budget(usage, Decimal("5"))


async def test_budget_stop_uses_provider_usage_before_calling_model(
    tmp_path, monkeypatch
):
    from scripts import run_literature_ablation as run

    class Policy:
        async def step(self, graph, status):
            raise AssertionError("A model call must not occur below the reserve")

    monkeypatch.setattr(run, "key_usage", lambda: {"limit": 5, "usage": 4})
    logged = []
    policy = TrackedPolicy(
        Policy(), tmp_path, Decimal("6"), SimpleNamespace(log=logged.append)
    )
    result = await policy.step([], RunStatus(2, 0, 0, 0, RunLimits(), run_id="test"))
    assert policy.stopped_for_budget
    assert result[0].role == "budget"
    assert logged[0]["provider_spend_usd"] == 4


async def test_headless_job_enforces_state_budget_saves_finalist_and_resumes(
    genes, tmp_path, monkeypatch, adk_model
):
    import hashlib
    import json

    from google.genai import types
    from yggdrisil import EvaluationResult, SQLiteStateGraph

    from scripts import run_literature_ablation as run
    from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
    from yggdrisil_ecoli.state import GenomeState, genome_state_key

    root = genome_state_key(GenomeState(frozenset({"b0001"})))

    continuing = False

    def respond(request):
        output = (
            {"plan": "Propose two candidates", "requests": [{"state_id": root}]}
            if "get_state" in request.tools_dict
            else {"actions": [{"genes": ["b0003"]}]}
            if continuing
            else {"actions": [{"genes": ["b0002"]}, {"genes": ["b0003"]}]}
        )
        return [types.Part(text=json.dumps(output))]

    model = adk_model(respond)
    # Source hydration is unrelated to this test's execution and resume budget.
    monkeypatch.setattr(run, "source_hash", lambda module: "fixture-source")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setattr(
        run, "key_usage", lambda: {"limit": 5, "usage": len(model.requests) * 0.001}
    )
    monkeypatch.setattr(run, "load_genes", lambda path: genes)
    modules = ModuleEvaluator(
        registry=genes,
        entries={},
        wt_complete_module_ids=(),
        parser_semantics_version="fixture",
    )
    monkeypatch.setattr(run.ModuleEvaluator, "from_json", lambda *args: modules)

    class Evidence:
        version = "fixture"
        config = "fixture"

        def __init__(self, name):
            self.name = name

        async def evaluate(self, state):
            return EvaluationResult(
                {"feasible": True, "growth_rate": 1.0}
                if self.name == "fba"
                else {"feasible_at_growth_floor": True}
            )

    monkeypatch.setattr(run, "FBAScorer", lambda **kwargs: Evidence("fba"))
    monkeypatch.setattr(
        run, "RBAScorer", lambda **kwargs: Evidence("resource_allocation")
    )
    archived = []

    class Artifact:
        qualified_name = "entity/project/test-data:v0"

        def __init__(self, *args, **kwargs):
            pass

        def add_file(self, path, name):
            assert __import__("pathlib").Path(path).exists()
            archived.append(name)

        def wait(self):
            return self

    class Tracking:
        url = "https://example.org/run"
        id = "fixture"

        def __init__(self):
            self.summary = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def log(self, data):
            pass

        def log_artifact(self, artifact):
            return artifact

    monkeypatch.setattr(run.wandb, "init", lambda **kwargs: Tracking())
    monkeypatch.setattr(run.wandb, "Artifact", Artifact)
    input_path = tmp_path / "input.json"
    input_path.write_text("{}")
    digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    spec = {
        "inputs": {
            key: {"path": "input.json", "sha256": digest}
            for key in ["genes", "modules", "fba_model", "rba_manifest"]
        },
        "agent": {"bundle_size": 2},
        "tracking": {"project": "test", "entity": "test", "tags": []},
    }
    job = {
        "run_id": "test",
        "arm": "no_retrieval",
        "seed": 59,
        "starting_gene_set_sha256": "fixture-start-hash",
        "live_literature": False,
        "graph_path": "search/test/graph.sqlite",
        "starting_genotype": "start.json",
        "dossier": None,
        "agent_config": {
            "model": "vendor/model",
            "mode": "tool-rich",
            "orchestration": "manager",
            "bundle_size": 2,
            "session_path": "search/test/agents.sqlite",
            "edison_cache_path": "search/test/edison-disabled.sqlite",
        },
        "limits": {"max_states": 2, "max_steps": 2, "max_wall_time_s": 60},
    }
    (tmp_path / "protocol.json").write_text(json.dumps(spec))
    (tmp_path / "start.json").write_text(
        json.dumps({"name": "test-root", "deleted_gene_ids": ["b0001"]})
    )
    monkeypatch.setattr(run, "load_job", lambda *args: (spec, job))
    await run.run_job(tmp_path, 0, tmp_path, Decimal("5"), False)
    report = json.loads((tmp_path / "search/test/result.json").read_text())
    assert report["result"]["unique_states"] == 2
    assert report["finalist"]["deleted_gene_ids"] == ["b0001", "b0002"]
    assert report["additional_deletions"] == 1
    progress = json.loads((tmp_path / "search/test/progress.json").read_text())
    assert progress["states"] == report["result"]["unique_states"]
    assert progress["step"] == report["result"]["step"]
    assert progress["status"] == "completed"
    assert "agents.sqlite" in archived and "graph.sqlite" in archived
    with SQLiteStateGraph(tmp_path / job["graph_path"]) as graph:
        assert len(graph) == 2
        assert any(
            event.outcome == "skipped_max_states"
            for event in graph.proposal_events(run_id="test")
        )
    requests = len(model.requests)
    await run.run_job(tmp_path, 0, tmp_path, Decimal("5"), True)
    assert len(model.requests) == requests

    # Fork the closed checkpoint into a new execution with a fresh key and budget.
    import shutil
    from copy import deepcopy

    parent_folder = tmp_path / "parents/test"
    parent_folder.mkdir(parents=True)
    parent_hashes = {}
    for name in ("config.json", "graph.sqlite", "agents.sqlite", "result.json"):
        source = tmp_path / "search/test" / name
        shutil.copyfile(source, parent_folder / name)
        parent_hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    continuing = True
    root = genome_state_key(GenomeState(frozenset({"b0001", "b0002"})))
    child = deepcopy(job)
    child.update(execution_id="stage-two", graph_path="search/stage-two/graph.sqlite")
    child["agent_config"].update(
        session_path="search/stage-two/agents.sqlite",
        edison_cache_path="search/stage-two/disabled.sqlite",
    )
    child["limits"].update(max_states=3, max_steps=report["result"]["step"] + 5)
    child["continuation"] = {
        "parent_path": "parents/test",
        "member_sha256": parent_hashes,
        "parent_states": 2,
        "parent_step": report["result"]["step"],
        "parent_best_deletions": 2,
        "additional_limits": {"max_states": 1, "max_steps": 5},
    }
    monkeypatch.setattr(run, "load_job", lambda *args: (spec, child))
    monkeypatch.setenv("OPENROUTER_API_KEY", "new-unused-test-key")
    monkeypatch.setattr(run, "key_usage", lambda: {"limit": 5, "usage": 0})
    await run.run_job(tmp_path, 0, tmp_path, Decimal("5"), False)
    continued = json.loads((tmp_path / "search/stage-two/result.json").read_text())
    assert continued["execution_id"] == "stage-two"
    assert continued["result"]["unique_states"] == 3
    assert continued["continuation"]["new_states"] == 1
    for name, digest in parent_hashes.items():
        assert hashlib.sha256((parent_folder / name).read_bytes()).hexdigest() == digest
    requests = len(model.requests)
    await run.run_job(tmp_path, 0, tmp_path, Decimal("5"), True)
    assert len(model.requests) == requests


async def test_continuation_fresh_research_gate_preserves_history(tmp_path):
    from google.adk.events import Event, EventActions

    from scripts.run_literature_ablation import continuation_memory

    path = tmp_path / "memory.sqlite"
    dossier = {"content": "Immutable original dossier"}
    await seed_memory(path, "parent", dossier)
    sessions = SqliteSessionService(str(path))
    try:
        session = await sessions.get_session(
            app_name="yggdrisil", user_id="parent", session_id="navigator"
        )
        await sessions.append_event(
            session=session,
            event=Event(
                author="navigator",
                invocation_id="old-search",
                actions=EventActions(
                    state_delta={
                        "user:literature_search_count": 21,
                        "user:plan": {"plan": "Keep the known promising branch"},
                        "user:documents": {"note": {"content": "Prior evidence"}},
                    }
                ),
            ),
        )
    finally:
        await sessions.close()
    assert await continuation_memory(path, "parent", "stage-two") == 0
    sessions = SqliteSessionService(str(path))
    try:
        session = await sessions.get_session(
            app_name="yggdrisil", user_id="parent", session_id="navigator"
        )
        assert session.state["user:initial_dossier"] == dossier
        assert session.state["user:continuation_parent_literature_search_count"] == 21
        assert session.state["user:plan"]["plan"] == "Keep the known promising branch"
        assert session.state["user:documents"]["note"]["content"] == "Prior evidence"
        assert len(session.events) == 2
        await sessions.append_event(
            session=session,
            event=Event(
                author="research",
                invocation_id="new-search",
                actions=EventActions(state_delta={"user:literature_search_count": 1}),
            ),
        )
    finally:
        await sessions.close()
    assert await continuation_memory(path, "parent", "stage-two") == 1
