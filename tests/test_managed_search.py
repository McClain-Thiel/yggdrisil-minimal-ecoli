import json
from dataclasses import replace
from urllib.error import HTTPError

import pytest
from yggdrisil import (
    EvaluationResult,
    EvaluatorSuite,
    RunLimits,
    Runner,
    SQLiteStateGraph,
)

from yggdrisil_ecoli.agent_policy import AgentSearchConfig, make_agent_policy
from yggdrisil_ecoli.analysis import summarize_run
from yggdrisil_ecoli.problem import EcoliProblem
from yggdrisil_ecoli.scorers.base import active_evaluator_ids
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools import literature
from yggdrisil_ecoli.tools.genes import GeneTools


def modules(genes):
    return ModuleEvaluator(
        registry=genes,
        entries={},
        wt_complete_module_ids=(),
        parser_semantics_version="fixture",
    )


def test_identifier_resolution_reports_ambiguity_and_unmatched_inputs(genes):
    genes = genes.copy()
    genes.loc[["b0001", "b0002"], "symbol"] = "shared"
    genes.loc["b0001", "kegg_gene_id"] = "eco:b0001"
    tools = GeneTools(genes, modules(genes))
    result = tools.resolve_gene_ids(["b0001", "SHARED", "absent", "eco:b0001"])
    expected = [
        {"input": "b0001", "status": "resolved", "gene_ids": ["b0001"]},
        {"input": "SHARED", "status": "ambiguous", "gene_ids": ["b0001", "b0002"]},
        {"input": "absent", "status": "unmatched", "gene_ids": []},
        {"input": "eco:b0001", "status": "resolved", "gene_ids": ["b0001"]},
    ]
    assert result == [
        {**row, "remaining_gene_ids": row["gene_ids"]} for row in expected
    ]
    tools.deleted_genes = frozenset({"b0001"})
    remaining = tools.resolve_gene_ids(["b0001", "SHARED"])
    assert remaining[0]["remaining_gene_ids"] == []
    assert remaining[1]["status"] == "ambiguous"
    assert remaining[1]["remaining_gene_ids"] == ["b0002"]
    with pytest.raises(ValueError, match="1 to 100"):
        tools.resolve_gene_ids(["b0001"] * 101)
    assert tools.deletion_delta(["b0001", "b0002"])["remaining_gene_ids"] == ["b0002"]
    with pytest.raises(ValueError, match="unknown candidate"):
        tools.deletion_delta(["b9999"])
    with pytest.raises(ValueError, match="closed-book"):
        replace(tools, aliases={}).resolve_gene_ids(["b0001"])


async def test_ordinary_literature_search_preserves_sources_and_pages_text(monkeypatch):
    def response(url):
        if "/search?" in url:
            assert "resultType=core" in url
            return json.dumps(
                {
                    "hitCount": 1,
                    "resultList": {
                        "result": [
                            {
                                "id": "123",
                                "source": "MED",
                                "doi": "10.example/paper",
                                "title": "Reduced genome",
                                "abstractText": "Test evidence",
                            }
                        ]
                    },
                }
            ).encode()
        assert url.endswith("/PMC123/fullTextXML")
        return (
            b"<article><title>Evidence</title><body><p>Full text</p></body></article>"
        )

    monkeypatch.setattr(literature, "_get", response)
    result = await literature.search_literature("MG1655")
    assert result["papers"][0]["doi"] == "10.example/paper"
    page = await literature.read_literature("PMC123", offset=9, count=4)
    assert page["text"] == "Full"
    assert page["source_url"].endswith("/PMC123/fullTextXML")

    def unavailable(url):
        raise HTTPError(url, 404, "not accessible", {}, None)

    monkeypatch.setattr(literature, "_get", unavailable)
    with pytest.raises(HTTPError):
        await literature.read_literature("PMC123")


def test_literature_retries_transient_http_errors_and_keeps_failures_visible(
    monkeypatch, caplog
):
    from contextlib import nullcontext
    from io import BytesIO

    attempts = []
    waits = []

    def response(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        return nullcontext(BytesIO(b"retrieved source"))

    monkeypatch.setattr(literature, "urlopen", response)
    monkeypatch.setattr(literature.time, "sleep", waits.append)
    assert literature._get("https://example.org/source") == b"retrieved source"
    assert len(attempts) == 3
    assert waits == [1, 2]
    assert "Europe PMC HTTP 503" in caplog.text

    def unavailable(request, timeout):
        raise HTTPError(request.full_url, 503, "unavailable", {}, None)

    monkeypatch.setattr(literature, "urlopen", unavailable)
    with pytest.raises(HTTPError):
        literature._get("https://example.org/source")


async def test_research_reports_search_outage_without_recording_evidence(
    genes, tmp_path, monkeypatch, caplog
):
    from types import SimpleNamespace

    from yggdrisil_ecoli.research import make_research_agent

    async def unavailable(query, count):
        raise HTTPError("https://example.org/search", 503, "unavailable", {}, None)

    monkeypatch.setattr(literature, "search_literature", unavailable)
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        child = make_research_agent(
            GeneTools(genes, modules(genes)),
            AgentSearchConfig(model="vendor/model"),
            graph.readonly(),
            None,
        )
        search = next(t for t in child.tools if t.__name__ == "search_literature")
        context = SimpleNamespace(state={})
        result = await search(query="MG1655", count=5, tool_context=context)
    assert "HTTP 503" in result["error"]
    assert not context.state.get("user:evidence_retrieved")
    assert not context.state.get("user:literature_search_count")
    assert "search unavailable" in caplog.text


def call(name, **args):
    from google.genai import types

    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def answer(value):
    from google.genai import types

    return types.Part(text=json.dumps(value))


def returns(request):
    return [
        p.function_response
        for c in request.contents
        for p in c.parts or []
        if p.function_response
    ]


@pytest.mark.parametrize("use_edison", [False, True])
async def test_native_team_walks_mg1655_and_restores_plan_documents_and_jobs(
    genes, tmp_path, monkeypatch, edison_service, use_edison, adk_model
):
    problem = EcoliProblem(genes)
    root = problem.state_key(GenomeState(frozenset()))
    child = problem.state_key(GenomeState(frozenset({"b0001"})))
    turns = []
    edison_service["status"] = "in progress"

    async def search(query, count=5):
        return {
            "source_url": "https://example.org/source",
            "papers": [{"title": "Test evidence"}],
        }

    monkeypatch.setattr(literature, "search_literature", search)

    def respond(request):
        names = set(request.tools_dict)
        responses = returns(request)
        if "search_literature" in names:
            if not responses:
                if use_edison and len(turns) == 1:
                    return [call("submit_edison", query="Focused MG1655 question")]
                return [call("search_literature", query="MG1655 deletion targets")]
            if use_edison and len(turns) == 1:
                return [answer({"note": "Job pending; continue genome exploration"})]
            if responses[-1].name != "save_shared_memory":
                return [
                    call(
                        "save_shared_memory",
                        key="targets/published",
                        content="Two coding targets from a cited source",
                        metadata={
                            "reference": "NC_000913.3",
                            "source_url": "https://example.org/source",
                            "gene_ids": ["b0001", "b0002"],
                        },
                    )
                ]
            assert "error" not in responses[-1].response
            return [answer({"note": "Saved canonical targets"})]
        if "get_state" in names or "research_literature" in names:
            if not responses:
                turns.append(len(turns))
                if len(turns) > 1:
                    instruction = str(request.config.system_instruction)
                    assert "Investigate cited deletion delta" in instruction
                    if not use_edison:
                        assert "targets/published" in instruction
                    else:
                        assert "formatted_answer" in instruction
                return [
                    call(
                        "research_literature",
                        request="Find focused MG1655 reduction evidence",
                    )
                ]
            return [
                answer(
                    {
                        "plan": "Investigate cited deletion delta, then extend it",
                        "requests": [{"state_id": root if len(turns) == 1 else child}],
                    }
                )
            ]
        assert "analyze_deletion_bundle" in names
        assert not names & {"search_literature", "submit_edison"}
        if len(turns) == 2 and not responses:
            return [
                call("compare_saved_gene_set", key="targets/published", state_id=child)
            ]
        if responses:
            assert responses[-1].response["remaining_gene_ids"] == ["b0002"]
        return [
            answer({"actions": [{"genes": ["b0001" if len(turns) == 1 else "b0002"]}]})
        ]

    model = adk_model(respond)

    class Evidence:
        version = "fixture"
        config = "offline"

        def __init__(self, name):
            self.name = name

        async def evaluate(self, state):
            metrics = {"genes_deleted": len(state.deleted_genes)}
            if self.name == "fba":
                metrics = {"feasible": True, "growth_rate": 1.0}
            if self.name == "resource_allocation":
                metrics = {"feasible_at_growth_floor": True}
            return EvaluationResult(metrics)

    scorers = [
        Evidence(name)
        for name in (
            "genome_size",
            "essentiality",
            "module_retention",
            "fba",
            "resource_allocation",
        )
    ]
    identities = active_evaluator_ids(scorers)
    config = AgentSearchConfig(
        model="vendor/model",
        mode="research",
        orchestration="manager",
        edison=use_edison,
        require_initial_research=not use_edison,
        session_path=tmp_path / "agents.sqlite",
        edison_cache_path=tmp_path / "edison.sqlite",
        max_model_requests=12,
    )
    path = tmp_path / "graph.sqlite"
    for iteration in range(2):
        with SQLiteStateGraph(path) as graph:
            policy = make_agent_policy(
                genes=genes,
                modules=modules(genes),
                config=config,
                evaluator_ids=identities,
                graph=graph.readonly(),
                run_id="managed",
            )
            await Runner(
                problem,
                policy,
                graph,
                RunLimits(max_steps=iteration + 1, max_states=3),
                evaluators=EvaluatorSuite(scorers),
                run_id="managed",
                metadata={"evaluators": identities},
            ).run()
            assert len(graph) == iteration + 2
            assert all(len(graph.evaluations(n.state_id)) == 5 for n in graph.states())
        edison_service["status"] = "success"
    summary = summarize_run(path)
    assert summary["deepest_viable_candidate"]["deleted_gene_ids"] == ["b0001", "b0002"]
    assert summary["model_usage"]["requests"] == len(model.requests)
    assert summary["scientific_tool_calls"]["research_literature"] == 2
    assert summary["decision_counts"].keys() == {"navigator", "explorer"}
    assert len(edison_service["submit"]) == int(use_edison)
    assert sum(not lite for _, lite in edison_service["poll"]) == int(use_edison)


@pytest.mark.parametrize("growth,resource_feasible", [(0.0, True), (1.0, False)])
async def test_manager_rejects_failed_growth_parent(
    genes, tmp_path, growth, resource_feasible, adk_model
):
    from pydantic import ValidationError
    from yggdrisil.agents import NavigatorContext
    from yggdrisil.limits import RunStatus

    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        graph.add_state("dead", GenomeState(frozenset()))
        for name, metrics in {
            "fba": {"feasible": True, "growth_rate": growth},
            "resource_allocation": {"feasible_at_growth_floor": resource_feasible},
        }.items():
            graph.add_evaluation(
                "dead",
                evaluator_id=name,
                evaluator=name,
                version="fixture",
                config_hash="fixture",
                result=EvaluationResult(metrics),
            )
        adk_model(
            lambda request: [
                answer({"plan": "Try this", "requests": [{"state_id": "dead"}]})
            ]
        )
        policy = make_agent_policy(
            genes=genes,
            modules=modules(genes),
            config=AgentSearchConfig(
                model="vendor/model",
                mode="tool-rich",
                orchestration="manager",
                session_path=tmp_path / "agents.sqlite",
            ),
            evaluator_ids={n: n for n in ("fba", "resource_allocation")},
            graph=graph.readonly(),
            run_id="run",
        )
        with pytest.raises(
            (RuntimeError, ValidationError), match="positive feasible FBA"
        ):
            await policy.navigator.plan(
                NavigatorContext(
                    None,
                    RunStatus(0, 1, 0, 0, RunLimits(), run_id="run"),
                    1,
                    0,
                    ["dead"],
                    [],
                    {},
                )
            )


async def test_shared_targets_require_sources_canonical_ids_and_registry_members(
    genes, tmp_path
):
    from types import SimpleNamespace

    from yggdrisil.agents import ToolInputError

    from yggdrisil_ecoli.research import memory_tools

    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        tools = {
            t.__name__: t
            for t in memory_tools(
                GeneTools(genes, modules(genes)), graph.readonly(), writable=True
            )
        }
        context = SimpleNamespace(state={})
        save = tools["save_shared_memory"]
        metadata = {
            "source_url": "https://example.org/source",
            "reference": "NC_000913.3",
            "gene_ids": ["b0001"],
        }
        with pytest.raises(ToolInputError, match="Retrieve actual"):
            save("targets/test", "Cited target", metadata, context)
        context.state["user:evidence_retrieved"] = True
        for invalid, message in [
            ({"canonical_gene_ids": ["b0001"]}, "metadata.gene_ids"),
            ({"gene_ids": ["b9999"]}, "unmatched"),
            ({"gene_ids": ["b0001", "b0001"]}, "unique"),
            ({"reference": "other"}, "NC_000913.3"),
        ]:
            merged = {**metadata, **invalid}
            if "canonical_gene_ids" in invalid:
                merged.pop("gene_ids")
            with pytest.raises(ToolInputError, match=message):
                save("targets/test", "Cited target", merged, context)
        save("targets/test", "Cited target", metadata, context)
        assert tools["list_shared_memory"](context) == ["targets/test"]
        assert (
            tools["read_shared_memory"]("targets/test", context)["metadata"] == metadata
        )
        with pytest.raises(ToolInputError, match="does not exist"):
            tools["read_shared_memory"]("invented", context)
        with pytest.raises(ToolInputError, match="literature/ or targets/"):
            save("edison/job", "Override raw report", metadata, context)


async def test_research_delegation_is_bounded_per_turn(genes, tmp_path, adk_model):
    from google.adk import Agent
    from pydantic import BaseModel
    from yggdrisil.agents.adk import AdkInvocation

    from yggdrisil_ecoli.research import make_research_agent

    count = []

    def respond(request):
        if "search_literature" in request.tools_dict:
            count.append(1)
            return [answer({"note": "No extra requests needed"})]
        if len(returns(request)) < 2:
            return [call("research_literature", request="Focused question")]
        return [answer({"note": "Research reused"})]

    model = adk_model(respond)

    class Output(BaseModel):
        note: str

    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        child = make_research_agent(
            GeneTools(genes, modules(genes)),
            AgentSearchConfig(model="vendor/model"),
            graph.readonly(),
            None,
        )
        manager = Agent(
            name="navigator",
            model=model,
            sub_agents=[child],
            output_schema=Output,
            output_key="result",
        )
        await AdkInvocation(manager).run("Research")
    assert count == [1]
    assert (
        sum(
            p.function_call is not None
            and p.function_call.name == "research_literature"
            for request in model.requests
            for content in request.contents
            for p in content.parts or []
        )
        >= 2
    )


@pytest.mark.parametrize("assign_work", [False, True])
async def test_manager_replans_after_empty_worker_and_can_explicitly_stop(
    genes, tmp_path, adk_model, assign_work
):
    root = EcoliProblem(genes).state_key(GenomeState(frozenset()))
    turns = []

    def respond(request):
        if "get_state" in request.tools_dict:
            turns.append(1)
            if len(turns) == 2:
                assert any(
                    "Need another planning turn" in (part.text or "")
                    for content in request.contents
                    for part in content.parts or []
                )
            return [
                answer(
                    {
                        "plan": "Review actual worker feedback",
                        "requests": [
                            {
                                "state_id": root,
                                "guidance": f"Planning turn {len(turns)}",
                            }
                        ]
                        if assign_work
                        else [],
                    }
                )
            ]
        return [
            answer(
                {
                    "actions": [] if len(turns) == 1 else [{"genes": ["b0001"]}],
                    "note": "Need another planning turn",
                }
            )
        ]

    class Growth:
        version = "fixture"
        config = "offline"

        def __init__(self, name):
            self.name = name

        async def evaluate(self, state):
            return EvaluationResult(
                {"feasible": True, "growth_rate": 1.0}
                if self.name == "fba"
                else {"feasible_at_growth_floor": True}
            )

    adk_model(respond)
    scorers = [Growth("fba"), Growth("resource_allocation")]
    identities = active_evaluator_ids(scorers)
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        policy = make_agent_policy(
            genes=genes,
            modules=modules(genes),
            config=AgentSearchConfig(
                model="vendor/model",
                mode="tool-rich",
                orchestration="manager",
                session_path=tmp_path / "agents.sqlite",
            ),
            evaluator_ids=identities,
            graph=graph.readonly(),
            run_id="empty-worker",
        )
        result = await Runner(
            EcoliProblem(genes),
            policy,
            graph,
            RunLimits(max_steps=2),
            evaluators=EvaluatorSuite(scorers),
            run_id="empty-worker",
        ).run()
        assert len(graph) == (2 if assign_work else 1)
    assert result.stop_reason == ("max_steps" if assign_work else "no_proposals")
    assert len(turns) == (2 if assign_work else 1)


@pytest.mark.parametrize("require_research", [False, True])
async def test_dossier_cannot_satisfy_required_fresh_research(
    genes, tmp_path, adk_model, require_research
):
    from yggdrisil.agents import NavigatorContext
    from yggdrisil.limits import RunStatus

    from scripts.run_literature_ablation import seed_memory

    dossier = {
        "content": "Frozen cited report",
        "source_url": "https://example.org/report",
    }
    sessions = tmp_path / "agents.sqlite"
    await seed_memory(sessions, "dossier-run", dossier)

    def respond(request):
        assert "Frozen cited report" in str(request.config.system_instruction)
        assert "get_state" in request.tools_dict
        assert "research_literature" in request.tools_dict
        return [
            answer(
                {
                    "plan": "Use the existing dossier; no unresolved question",
                    "requests": [],
                }
            )
        ]

    model = adk_model(respond)
    with SQLiteStateGraph(tmp_path / "graph.sqlite") as graph:
        policy = make_agent_policy(
            genes=genes,
            modules=modules(genes),
            config=AgentSearchConfig(
                model="vendor/model",
                mode="research",
                orchestration="manager",
                require_initial_research=require_research,
                session_path=sessions,
            ),
            evaluator_ids={name: name for name in ("fba", "resource_allocation")},
            graph=graph.readonly(),
            run_id="dossier-run",
        )
        context = NavigatorContext(
            None,
            RunStatus(0, 0, 0, 0, RunLimits(), run_id="dossier-run"),
            0,
            0,
            [],
            [],
            {},
        )
        if require_research:
            with pytest.raises(ValueError, match="successful Europe PMC search"):
                await policy.navigator.plan(context)
        else:
            plan = await policy.navigator.plan(context)
            assert plan.requests == []
    assert len(model.requests) == 1
