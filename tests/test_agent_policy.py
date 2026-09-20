import json
from contextlib import ExitStack
from pathlib import Path

import pytest
from pydantic import ValidationError
from yggdrisil.agents import ExplorerContext

from yggdrisil_ecoli.actions import DeleteGenes
from yggdrisil_ecoli.agent_policy import (
    AgentSearchConfig,
    _action_type,
    _GeneTools,
    _GeneView,
    _LimitedAgent,
    make_agent_policy,
)
from yggdrisil_ecoli.data.essentiality import EssentialityDataset, EssentialityRecord
from yggdrisil_ecoli.data.gff import parse_ncbi_gff
from yggdrisil_ecoli.scorers.modules import ModuleEvaluator
from yggdrisil_ecoli.state import GenomeState
from yggdrisil_ecoli.tools.genes import GeneTools


@pytest.fixture
def evidence() -> GeneTools:
    registry = parse_ncbi_gff(
        Path(__file__).parent / "fixtures/mg1655_excerpt.gff3"
    ).registry
    return GeneTools(
        registry=registry,
        essentiality=EssentialityDataset(
            EssentialityRecord(b_number=gene) for gene in registry.search_universe
        ),
        modules=ModuleEvaluator(
            registry=registry,
            entries={},
            wt_complete_module_ids=(),
            parser_semantics_version="fixture",
        ),
    )


def context(deleted=frozenset()) -> ExplorerContext:
    return ExplorerContext(
        goal="minimize",
        state_id="fixture",
        state=GenomeState(deleted),
        lineage=[],
        guidance=None,
    )


def policy(evidence, config, monkeypatch):
    from pydantic_ai import models

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", False)
    return make_agent_policy(
        registry=evidence.registry,
        essentiality=evidence.essentiality,
        modules=evidence.modules,
        config=config,
        evaluator_ids={},
        evaluations=lambda state_id: [],
    )


def test_agent_config_and_mapping_are_reproducible_and_secret_free(evidence):
    config = AgentSearchConfig(model="openai/gpt-4o-mini-2024-07-18", seed=7)
    metadata = config.metadata(evidence.registry)
    assert metadata == config.metadata(evidence.registry)
    assert metadata["blind_map_sha256"]
    assert "key" not in str(metadata).lower()
    assert _GeneView(evidence.registry, config).aliases == {
        "b0001": "g0001",
        "b0003": "g0002",
        "b0002": "g0003",
    }
    with pytest.raises(ValueError, match="fixed OpenRouter model"):
        AgentSearchConfig(model="openrouter/free")


@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
def test_pydantic_action_validation_and_translation(evidence, mode):
    view = _GeneView(
        evidence.registry, AgentSearchConfig(model="vendor/model", mode=mode)
    )
    action = _action_type(view, GenomeState(frozenset({"b0001"})), 2)
    first, second = view.aliases["b0002"], view.aliases["b0003"]
    assert action(genes=(second, first)).genes == ("b0002", "b0003")
    for invalid, message in [
        ((first, second, first), "at most 2"),
        ((first, first), "duplicate"),
        ((view.aliases["b0001"],), "already deleted"),
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
    tools = _GeneTools(evidence, agent.explorer.view, current.state)
    preview = tools.list_deletion_candidates()
    aliases = agent.explorer.view.aliases
    bundle = tools.analyze_deletion_bundle([aliases["b0002"]])
    assert aliases["b0001"] in json.loads(prompt)["deleted_gene_ids_first_64"]
    assert preview["remaining_candidates"] == 2
    if mode == "closed-book":
        for leaked in ("b0001", "b0002", "b0003", "thrL", "thrA", "thrB"):
            assert leaked not in prompt + str(preview) + str(bundle)
        with pytest.raises(ValueError, match="unavailable"):
            tools.inspect_kegg_module("M00001")
    else:
        assert "thrA" in prompt
        assert bundle["valid_genes"] == ["b0001", "b0002"]
        assert bundle["proposed_gene_ids"] == ["b0002"]


@pytest.mark.asyncio
async def test_provider_errors_propagate_with_limits():
    limits = object()

    class FailingAgent:
        async def run(self, prompt, *, usage_limits):
            assert usage_limits is limits
            raise ValueError("provider failed")

    with pytest.raises(ValueError, match="provider failed"):
        await _LimitedAgent(FailingAgent(), limits).run("prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
async def test_native_framework_adapter_validates_actions_and_keeps_usage_trace(
    evidence, monkeypatch, mode
):
    from pydantic_ai.exceptions import CostNotFoundWarning
    from pydantic_ai.models.test import TestModel
    from yggdrisil.agents import pydantic_ai as adapter

    config = AgentSearchConfig(model="vendor/model", mode=mode, seed=7)
    agent = policy(evidence, config, monkeypatch)
    model = TestModel(
        call_tools=[],
        custom_output_args={
            "actions": [{"genes": [agent.explorer.view.aliases["b0002"]]}],
        },
    )
    make_explorer = adapter.make_explorer
    with ExitStack() as stack, pytest.warns(CostNotFoundWarning):

        def offline_explorer(*args, **kwargs):
            explorer = make_explorer(*args, **kwargs)
            stack.enter_context(explorer.agent.override(model=model))
            return explorer

        monkeypatch.setattr(adapter, "make_explorer", offline_explorer)
        result = await agent.explorer.explore(context())
    assert result.actions == [DeleteGenes(genes=("b0002",))]
    assert {
        tool.name for tool in model.last_model_request_parameters.function_tools
    } == set(config.tool_names)
    assert any(event["role"] == "usage" for event in result.trace)
    assert agent.explorer.limits.request_limit == config.max_model_requests
    assert agent.explorer.limits.cost_limit == config.max_cost_per_call_usd
