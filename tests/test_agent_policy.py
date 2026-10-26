import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from yggdrisil.agents import ExplorerContext
from yggdrisil.types import EvaluationRecord

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.agent_policy import (
    AgentSearchConfig,
    _action_type,
    _aliases,
    make_agent_policy,
)
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools.genes import GeneTools


@pytest.fixture
def evidence(genes) -> GeneTools:
    return GeneTools(
        genes,
        ModuleEvaluator(
            registry=genes,
            entries={},
            wt_complete_module_ids=(),
            parser_semantics_version="fixture",
        ),
    )


@pytest.fixture
def offline_model(monkeypatch):
    """Use real PydanticAI validation and limits with an in-process model."""
    from yggdrisil.agents import pydantic_ai as adapter

    make_explorer = adapter.make_explorer

    def install(model):
        def create(*args, **kwargs):
            explorer = make_explorer(*args, **kwargs)
            explorer.agent.model = model
            return explorer

        monkeypatch.setattr(adapter, "make_explorer", create)

    return install


def context(deleted=frozenset()) -> ExplorerContext:
    return ExplorerContext(
        goal="minimize",
        state_id="fixture",
        state=GenomeState(deleted),
        lineage=[],
        guidance=None,
        evaluations=[
            EvaluationRecord(
                "evaluation",
                "fba-fixture",
                "fixture",
                "fba",
                "fixture",
                "fixture",
                {"feasible": True, "growth_rate": 1.0},
            ),
            EvaluationRecord(
                "resource-evaluation",
                "resource-fixture",
                "fixture",
                "resource_allocation",
                "fixture",
                "fixture",
                {"feasible_at_growth_floor": True, "growth_rate_floor_h": 0.1},
            ),
        ],
    )


def policy(evidence, config, monkeypatch):
    from pydantic_ai import models

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    return make_agent_policy(
        genes=evidence.genes,
        modules=evidence.modules,
        config=config,
        evaluator_ids={"fba": "fba-fixture", "resource_allocation": "resource-fixture"},
    )


def test_agent_config_and_mapping_are_reproducible_and_secret_free(evidence):
    config = AgentSearchConfig(model="openai/gpt-4o-mini-2024-07-18", seed=7)
    metadata = config.metadata(evidence.genes)
    assert metadata == config.metadata(evidence.genes)
    assert metadata["blind_map_sha256"]
    assert "key" not in str(metadata).lower()
    assert _aliases(evidence.genes, config.seed) == {
        "b0001": "g0001",
        "b0003": "g0002",
        "b0002": "g0003",
    }
    with pytest.raises(ValueError, match="fixed OpenRouter model"):
        AgentSearchConfig(model="openrouter/free")


@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
def test_pydantic_action_validation_and_translation(evidence, mode):
    tools = replace(
        evidence,
        deleted_genes=frozenset({"b0001"}),
        aliases=_aliases(evidence.genes, 0) if mode == "closed-book" else None,
    )
    action = _action_type(tools, 2)
    first, second = tools.public("b0002"), tools.public("b0003")
    assert action(genes=(second, first)).genes == ("b0002", "b0003")
    for invalid, message in [
        ((first, second, first), "at most 2"),
        ((first, first), "duplicate"),
        ((tools.public("b0001"),), "already deleted"),
        (("unknown",), "unknown candidate"),
        ((), "at least 1"),
    ]:
        with pytest.raises(ValidationError, match=message):
            action(genes=invalid)
    if mode == "closed-book":
        with pytest.raises(ValidationError, match="unknown candidate"):
            action(genes=("b0002",))
        with pytest.raises(ValidationError) as error:
            action(genes=(first, first))
        assert "b0002" not in str(error.value)


@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
def test_prompt_and_tools_obey_evidence_arm(evidence, monkeypatch, mode):
    config = AgentSearchConfig(model="vendor/model", mode=mode, seed=11)
    agent = policy(evidence, config, monkeypatch)
    current = context(frozenset({"b0001"}))
    prompt = agent.explorer.format_prompt(current)
    tools = replace(agent.explorer.evidence, deleted_genes=current.state.deleted_genes)
    preview = tools.list_deletion_candidates()
    bundle = tools.analyze_deletion_bundle([tools.public("b0002")])
    assert tools.public("b0001") in json.loads(prompt)["deleted_gene_ids_first_64"]
    assert preview["remaining_candidates"] == 2
    if mode == "closed-book":
        for leaked in ("b0001", "b0002", "b0003", "thrL", "thrA", "thrB"):
            assert leaked not in prompt + str(preview) + str(bundle)
        with pytest.raises(ValueError, match="unavailable"):
            tools.inspect_kegg_module("M00001")
    else:
        assert "thrA" in prompt
        assert bundle["deleted_gene_ids"] == ["b0001", "b0002"]
        assert bundle["proposed_gene_ids"] == ["b0002"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
async def test_native_framework_adapter_validates_actions_and_keeps_usage_trace(
    evidence, monkeypatch, mode, offline_model
):
    from pydantic_ai.exceptions import CostNotFoundWarning
    from pydantic_ai.models.test import TestModel

    config = AgentSearchConfig(model="vendor/model", mode=mode, seed=7)
    agent = policy(evidence, config, monkeypatch)
    model = TestModel(
        call_tools=[],
        custom_output_args={
            "actions": [{"genes": [agent.explorer.evidence.public("b0002")]}],
        },
    )
    offline_model(model)
    with pytest.warns(CostNotFoundWarning):
        result = await agent.explorer.explore(context())
    assert result.actions == [DeleteGenes(genes=("b0002",))]
    assert {
        tool.name for tool in model.last_model_request_parameters.function_tools
    } == set(config.tool_names)
    assert any(event["role"] == "usage" for event in result.trace)
    assert agent.explorer.limits.request_limit == config.max_model_requests
    assert agent.explorer.limits.cost_limit == config.max_cost_per_call_usd


def test_consolidated_tools_report_cumulative_module_status_and_coverage(genes):
    genes = genes.copy()
    for gene in ("b0001", "b0002"):
        genes.at[gene, "ko_ids"] = ("K00001",)
    genes.loc[:, "iml1515_gene_id"] = None
    genes.at["b0001", "iml1515_gene_id"] = "b0001"
    modules = ModuleEvaluator(
        registry=genes,
        entries={"M00001": {"name": "Isozymes", "definition": "K00001"}},
        wt_complete_module_ids=("M00001",),
        parser_semantics_version="fixture",
    )
    tools = GeneTools(genes, modules, deleted_genes=frozenset({"b0001"}))
    assert tools.inspect_kegg_module("M00001")["complete_after_deletion"] is True
    assert (
        tools.inspect_kegg_module("M00001", ["b0002"])["complete_after_deletion"]
        is False
    )
    result = tools.analyze_deletion_bundle(["b0002", "b0002"])
    assert result["deleted_genes_total"] == 2
    assert result["modules_broken"] == 1
    assert result["broken_module_ids"] == ("M00001",)
    assert result["essentiality"] == {"nonessential": 2}
    assert result["model_coverage"] == {"modeled": 1, "unmodeled": 1}
    assert result["ko_coverage"] == {"mapped": 2, "unmapped": 0}


@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
def test_invocation_exposure_and_bundle_limits(evidence, mode):
    tools = replace(
        evidence,
        aliases=_aliases(evidence.genes, 0) if mode == "closed-book" else None,
        exposed_ids=set(),
        max_bundle_size=2,
    )
    first, second = tools.public("b0001"), tools.public("b0002")
    tools.list_deletion_candidates(count=1)
    assert tools.inspect_gene_evidence(first)["gene_id"] == first
    for operation in (
        lambda: tools.inspect_gene_evidence(second),
        lambda: tools.analyze_deletion_bundle([second]),
        lambda: _action_type(tools, 2)(genes=(second,)),
    ):
        with pytest.raises(ValueError, match="not exposed"):
            operation()
    tools.list_deletion_candidates(page=1, count=1)
    assert _action_type(tools, 2)(genes=(second, first)).genes == ("b0001", "b0002")
    for invalid, message in [
        ([], "1 to 2"),
        ([first] * 3, "1 to 2"),
        ([first] * 2, "duplicate"),
    ]:
        with pytest.raises(ValueError, match=message):
            tools.analyze_deletion_bundle(invalid)


@pytest.mark.asyncio
async def test_native_output_keeps_variable_sizes_and_deduplicates_siblings(
    evidence, monkeypatch, offline_model
):
    from pydantic_ai.exceptions import CostNotFoundWarning
    from pydantic_ai.models.test import TestModel

    agent = policy(
        evidence,
        AgentSearchConfig(model="vendor/model", bundle_size=3, max_actions=4),
        monkeypatch,
    )
    agent.request_selector._attempted = {"fixture": frozenset({("b0001",)})}
    public = agent.explorer.evidence.public
    model = TestModel(
        call_tools=[],
        custom_output_args={
            "actions": [
                {"genes": [public("b0001")]},
                {"genes": [public("b0002"), public("b0003")]},
                {"genes": [public("b0003"), public("b0002")]},
                {"genes": [public("b0003")]},
            ]
        },
    )
    offline_model(model)
    with pytest.warns(CostNotFoundWarning):
        result = await agent.explorer.explore(context())
    assert result.actions == [
        DeleteGenes(genes=("b0002", "b0003")),
        DeleteGenes(genes=("b0003",)),
    ]
    assert "omitted 2 duplicate" in result.note
    assert "b000" not in json.dumps(result.trace)


@pytest.mark.asyncio
async def test_rotating_preview_does_not_share_exposure_between_invocations(
    genes, monkeypatch, offline_model
):
    import pandas as pd
    from pydantic_ai.exceptions import CostNotFoundWarning, UnexpectedModelBehavior
    from pydantic_ai.models.test import TestModel

    genes = pd.concat([genes.iloc[[0]]] * 16, ignore_index=True)
    genes.index = [f"b{index:04d}" for index in range(16)]
    evidence = GeneTools(
        genes,
        ModuleEvaluator(
            registry=genes,
            entries={},
            wt_complete_module_ids=(),
            parser_semantics_version="fixture",
        ),
    )
    agent = policy(evidence, AgentSearchConfig(model="vendor/model"), monkeypatch)
    first = context()
    second = replace(first, guidance=json.dumps({"candidate_preview_page": 1}))
    preview = json.loads(agent.explorer.format_prompt(first))["candidate_preview"]
    gene_id = preview["candidates"][0]["gene_id"]
    assert gene_id not in {
        row["gene_id"]
        for row in json.loads(agent.explorer.format_prompt(second))[
            "candidate_preview"
        ]["candidates"]
    }
    model = TestModel(
        call_tools=[], custom_output_args={"actions": [{"genes": [gene_id]}]}
    )
    offline_model(model)
    with pytest.warns(CostNotFoundWarning):
        result = await agent.explorer.explore(first)
        assert len(result.actions) == 1
        with pytest.raises(UnexpectedModelBehavior, match="retries"):
            await agent.explorer.explore(second)
    assert agent.explorer.evidence.exposed_ids is None


@pytest.mark.asyncio
async def test_native_request_budget_stops_validation_retry(
    evidence, monkeypatch, offline_model
):
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    calls = []

    def invalid_output(messages, info):
        calls.append(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "actions": [{"genes": ["never-exposed"]}],
                    },
                )
            ]
        )

    config = AgentSearchConfig(model="vendor/model", max_model_requests=1)
    agent = policy(evidence, config, monkeypatch)
    offline_model(FunctionModel(invalid_output))
    with pytest.raises(UsageLimitExceeded, match="request_limit"):
        await agent.explorer.explore(context())
    assert len(calls) == 1
