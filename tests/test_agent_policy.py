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
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
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


def test_model_seed_can_be_omitted_without_changing_candidate_order(evidence):
    seeded = AgentSearchConfig(model="vendor/model", seed=59)
    unseeded = seeded.model_copy(update={"send_model_seed": False})
    assert seeded.settings["seed"] == 59
    assert "seed" not in unseeded.settings
    assert (
        seeded.metadata(evidence.genes)["candidate_order_sha256"]
        == unseeded.metadata(evidence.genes)["candidate_order_sha256"]
    )
    assert unseeded.settings["extra_body"]["provider"]["require_parameters"]


def test_reasoning_effort_is_explicit_and_preserved_in_metadata(evidence):
    default = AgentSearchConfig(model="vendor/model")
    assert "reasoning" not in default.settings["extra_body"]
    config = AgentSearchConfig(model="vendor/model", reasoning_effort="low")
    assert config.settings["extra_body"]["reasoning"] == {"effort": "low"}
    assert config.metadata(evidence.genes)["reasoning_effort"] == "low"
    assert config.settings["extra_body"]["provider"]["require_parameters"]


def test_managed_explorer_uses_preview_instead_of_repeated_candidate_paging():
    config = AgentSearchConfig(
        model="vendor/model", mode="tool-rich", orchestration="manager"
    )
    assert "list_deletion_candidates" not in config.tool_names
    assert {"resolve_gene_ids", "inspect_genes", "analyze_deletion_bundle"} <= set(
        config.tool_names
    )


def test_large_deletion_batches_keep_identifier_and_size_checks(evidence):
    import pandas as pd

    genes = pd.concat([evidence.genes.iloc[[0]]] * 101, ignore_index=True)
    genes.index = [f"b{index:04d}" for index in range(1000, 1101)]
    tools = replace(evidence, genes=genes, max_bundle_size=100)
    config = AgentSearchConfig(model="vendor/model", bundle_size=100)
    preview = tools.list_deletion_candidates(count=config.candidate_preview_count)
    assert len(preview["candidates"]) == 100
    assert tools.inspect_genes(list(genes.index[:100])) == preview["candidates"]
    with pytest.raises(ValueError, match="1 to 100"):
        tools.inspect_genes(list(genes.index))
    action_type = _action_type(tools, config.bundle_size)
    assert len(action_type(genes=tuple(genes.index[:100])).genes) == 100
    with pytest.raises(ValidationError, match="at most 100"):
        action_type(genes=tuple(genes.index))
    with pytest.raises(ValueError, match="Initial research requires research mode"):
        AgentSearchConfig(model="vendor/model", require_initial_research=True)


def test_batch_inspection_reports_bad_ids_without_losing_valid_rows(evidence):
    result = evidence.inspect_genes(["b0001", "b9999", "b0002"])
    assert result[0] == evidence.inspect_gene_evidence("b0001")
    assert "unknown candidate" in result[1]["error"]
    assert result[2] == evidence.inspect_gene_evidence("b0002")
    with pytest.raises(ValueError, match="1 to 100"):
        evidence.inspect_genes([])
    limited = replace(evidence, exposed_ids={"b0001"})
    assert "not exposed" in limited.inspect_genes(["b0002"])[0]["error"]


def test_inspection_budget_filters_deleted_and_is_shared_with_candidate_pages(evidence):
    tools = replace(
        evidence, deleted_genes=frozenset({"b0001"}), max_gene_inspections=1
    )
    assert tools.inspect_genes(["b0001"])[0]["status"] == "already_deleted"
    assert "unknown candidate" in tools.inspect_genes(["invalid"])[0]["error"]
    assert tools.remaining_gene_inspections == 1
    preview = tools.list_deletion_candidates(count=1)
    assert preview["candidates"][0]["gene_id"] == "b0002"
    assert preview["remaining_gene_inspections"] == 0
    assert tools.inspect_genes(["b0002"])[0]["status"] == "already_inspected"
    assert "limit reached" in tools.inspect_genes(["b0003"])[0]["error"]
    assert (
        "limit reached"
        in tools.list_deletion_candidates(page=1, count=1)["candidates"][0]["error"]
    )
    assert tools.analyze_deletion_bundle(["b0002"])["deleted_genes_total"] == 2
    assert _action_type(tools, 1)(genes=("b0002",)).genes == ("b0002",)
    fresh = replace(tools)
    assert fresh.remaining_gene_inspections == 1
    assert "classification" in fresh.inspect_genes(["b0003"])[0]


def test_inspection_does_not_hide_infrastructure_errors(evidence, monkeypatch):
    def fail(gene_id):
        raise RuntimeError("Evidence table unavailable")

    monkeypatch.setattr(evidence, "inspect_gene_evidence", fail)
    with pytest.raises(RuntimeError, match="Evidence table unavailable"):
        evidence.inspect_genes(["b0001"])


@pytest.mark.asyncio
async def test_explorer_returns_action_after_inspection_and_tool_budgets(
    evidence, monkeypatch, adk_model
):
    from google.genai import types

    config = AgentSearchConfig(
        model="vendor/model", mode="tool-rich", max_gene_inspections=3, max_tool_calls=2
    )
    agent = policy(evidence, config, monkeypatch)
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return [
                types.Part(
                    function_call=types.FunctionCall(
                        name="inspect_genes", args={"gene_ids": ["b0002"]}
                    )
                )
            ]
        assert set(request.tools_dict) == {"set_model_response"}
        responses = [
            p.function_response
            for c in request.contents
            for p in c.parts or []
            if p.function_response
        ]
        assert "already_inspected" in str(responses)
        return [
            types.Part(
                function_call=types.FunctionCall(
                    name="set_model_response", args={"actions": [{"genes": ["b0002"]}]}
                )
            )
        ]

    adk_model(respond)
    result = await agent.explorer.explore(context())
    assert result.actions == [DeleteGenes(genes=("b0002",))]
    assert calls == 3


@pytest.mark.parametrize("mode", ["closed-book", "tool-rich"])
def test_pydantic_action_validation_and_translation(evidence, mode):
    tools = replace(
        evidence,
        deleted_genes=frozenset({"b0001"}),
        aliases=_aliases(evidence.genes, 0) if mode == "closed-book" else None,
    )
    action = _action_type(tools, 2)
    first, second = tools.public("b0002"), tools.public("b0003")
    assert action(genes=(second, first)).genes == (first, second)
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
async def test_native_framework_validates_actions_and_keeps_usage_trace(
    evidence, monkeypatch, mode, adk_model
):
    from google.genai import types

    config = AgentSearchConfig(model="vendor/model", mode=mode, seed=7)
    agent = policy(evidence, config, monkeypatch)
    model = adk_model(
        lambda request: [
            types.Part(
                text=json.dumps(
                    {"actions": [{"genes": [agent.explorer.evidence.public("b0002")]}]}
                )
            )
        ]
    )
    result = await agent.explorer.explore(context())
    assert result.actions == [DeleteGenes(genes=("b0002",))]
    assert set(config.tool_names) <= set(model.requests[0].tools_dict)
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
    assert _action_type(tools, 2)(genes=(second, first)).genes == (first, second)
    for invalid, message in [
        ([], "1 to 2"),
        ([first] * 3, "1 to 2"),
        ([first] * 2, "duplicate"),
    ]:
        with pytest.raises(ValueError, match=message):
            tools.analyze_deletion_bundle(invalid)


@pytest.mark.asyncio
async def test_native_output_keeps_variable_sizes_and_deduplicates_siblings(
    evidence, monkeypatch, adk_model
):
    from google.genai import types

    agent = policy(
        evidence,
        AgentSearchConfig(model="vendor/model", bundle_size=3, max_actions=4),
        monkeypatch,
    )
    agent.request_selector._attempted = {"fixture": frozenset({("b0001",)})}
    public = agent.explorer.evidence.public
    adk_model(
        lambda request: [
            types.Part(
                text=json.dumps(
                    {
                        "actions": [
                            {"genes": [public("b0001")]},
                            {"genes": [public("b0002"), public("b0003")]},
                            {"genes": [public("b0003"), public("b0002")]},
                            {"genes": [public("b0003")]},
                        ]
                    }
                )
            )
        ]
    )
    result = await agent.explorer.explore(context())
    assert result.actions == [
        DeleteGenes(genes=("b0002", "b0003")),
        DeleteGenes(genes=("b0003",)),
    ]
    assert "omitted 2 duplicate" in result.note
    assert "b000" not in json.dumps(result.trace)


@pytest.mark.asyncio
async def test_rotating_preview_does_not_share_exposure_between_invocations(
    genes, monkeypatch, adk_model
):
    import pandas as pd
    from google.genai import types

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
    adk_model(
        lambda request: [
            types.Part(text=json.dumps({"actions": [{"genes": [gene_id]}]}))
        ]
    )
    result = await agent.explorer.explore(first)
    assert len(result.actions) == 1
    with pytest.raises((RuntimeError, ValidationError), match="not exposed"):
        await agent.explorer.explore(second)
    assert agent.explorer.evidence.exposed_ids is None


@pytest.mark.asyncio
async def test_native_request_budget_stops_validation_retry(
    evidence, monkeypatch, adk_model
):
    from google.genai import types

    calls = []

    def invalid_output(request):
        calls.append(request)
        return [
            types.Part(
                function_call=types.FunctionCall(
                    name="set_model_response",
                    args={"actions": [{"genes": ["never-exposed"]}]},
                )
            )
        ]

    config = AgentSearchConfig(model="vendor/model", max_model_requests=1)
    agent = policy(evidence, config, monkeypatch)
    adk_model(invalid_output)
    with pytest.raises(
        (RuntimeError, ValidationError), match="limit|exposed|candidate"
    ):
        await agent.explorer.explore(context())
    assert len(calls) == 1
