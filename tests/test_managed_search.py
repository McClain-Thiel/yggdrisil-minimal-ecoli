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

from yggdrisil_ecoli.actions import DeleteGenes
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
    assert result == [
        {"input": "b0001", "status": "resolved", "gene_ids": ["b0001"]},
        {"input": "SHARED", "status": "ambiguous", "gene_ids": ["b0001", "b0002"]},
        {"input": "absent", "status": "unmatched", "gene_ids": []},
        {"input": "eco:b0001", "status": "resolved", "gene_ids": ["b0001"]},
    ]
    tools.deleted_genes = frozenset({"b0001"})
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


async def test_managed_search_walks_from_mg1655_and_resumes_without_paid_calls(
    genes,
    tmp_path,
    monkeypatch,
):
    from pydantic_ai import models
    from pydantic_ai.exceptions import CostNotFoundWarning
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from yggdrisil.agents import pydantic_ai as adapter

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    problem = EcoliProblem(genes, max_genes_per_action=1)
    root = problem.state_key(problem.initial_state)
    first = problem.state_key(GenomeState(frozenset({"b0001"})))
    calls = []

    def manager_response(messages, info):
        step = len(calls) + 1
        calls.append(messages)
        arguments = {
            "requests": [
                {
                    "state_id": root if step == 1 else first,
                    "guidance": "Follow the cited target deletion set",
                }
            ],
            "checkpoint": {"strategy": "reconstruct cited gene set", "step": step},
        }
        if step == 1:
            arguments["memory_updates"] = [
                {
                    "key": "published-target",
                    "content": "A fixture source lists two deletions",
                    "metadata": {
                        "reference": "NC_000913.3",
                        "gene_ids": ["b0001", "b0002"],
                        "source_url": "https://example.org/fixture",
                    },
                }
            ]
        else:
            assert "published-target" in str(messages)
            assert "created" in str(messages)
            assert "reconstruct cited gene set" in str(messages)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, arguments)])

    def worker_response(messages, info):
        gene = "b0001" if len(calls) == 1 else "b0002"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "actions": [{"genes": [gene]}],
                        "note": "proposed one target deletion",
                    },
                )
            ]
        )

    create_manager, create_worker = adapter.make_navigator, adapter.make_explorer

    def manager(*args, **kwargs):
        agent = create_manager(*args, **kwargs)
        agent.agent.model = FunctionModel(manager_response)
        return agent

    def worker(*args, **kwargs):
        agent = create_worker(*args, **kwargs)
        agent.agent.model = FunctionModel(worker_response)
        return agent

    monkeypatch.setattr(adapter, "make_navigator", manager)
    monkeypatch.setattr(adapter, "make_explorer", worker)

    class Evidence:
        version = "fixture"
        config = "offline"

        def __init__(self, name):
            self.name = name

        async def evaluate(self, state):
            metrics = {"genes_deleted": len(state.deleted_genes)}
            if self.name == "fba":
                metrics = {"feasible": True, "growth_rate": 1.0}
            elif self.name == "resource_allocation":
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
        model="vendor/model", mode="research", orchestration="manager"
    )
    path = tmp_path / "managed.sqlite"
    for steps in (1, 2):
        with SQLiteStateGraph[GenomeState, DeleteGenes](path) as graph:
            policy = make_agent_policy(
                genes=genes,
                modules=modules(genes),
                config=config,
                evaluator_ids=identities,
                graph=graph.readonly(),
                run_id="managed",
            )
            assert policy.navigator.model_settings["max_tokens"] == (
                config.max_manager_output_tokens
            )
            assert policy.explorer.limits.output_tokens_limit == (
                config.max_model_requests * config.max_output_tokens
            )
            with pytest.warns(CostNotFoundWarning):
                await Runner(
                    problem,
                    policy,
                    graph,
                    RunLimits(max_steps=steps),
                    evaluators=EvaluatorSuite(scorers),
                    run_id="managed",
                    metadata={"evaluators": identities},
                ).run()
            assert len(graph) == steps + 1
            assert graph.memory("managed", "published-target").revision == 1
    summary = summarize_run(path)
    assert summary["deepest_viable_candidate"]["deleted_gene_ids"] == ["b0001", "b0002"]
    assert len(calls) == 2
